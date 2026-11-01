"""
Tests for GraphQL schema diff engine.
"""
import sys
sys.path.insert(0, '.')

from app.graphql_diff import diff_graphql, parse_graphql, format_graphql_changes


def test_parse_graphql():
    schema = '''
type User {
  id: ID!
  name: String!
  email: String
  posts: [Post!]!
}

type Post {
  id: ID!
  title: String!
  author: User!
}

enum Status {
  ACTIVE
  INACTIVE
  BANNED
}
'''
    types = parse_graphql(schema)
    assert "User" in types
    assert "Post" in types
    assert "Status" in types
    assert len(types["User"].fields) == 4
    assert types["User"].fields["id"].nullable == False
    assert types["User"].fields["email"].nullable == True
    assert "ACTIVE" in types["Status"].enum_values
    print("✅ test_parse_graphql passed")


def test_field_removed():
    old = '''
type User {
  id: ID!
  name: String!
  email: String!
  age: Int
}
'''
    new = '''
type User {
  id: ID!
  name: String!
  email: String!
}
'''
    changes = diff_graphql(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "field_removed"
    assert changes[0].field_name == "age"
    print("✅ test_field_removed passed")


def test_field_made_required():
    # INPUT field nullable -> non-null: clients that omit it are rejected.
    old = '''
input CreateUserInput {
  name: String!
  email: String
}
'''
    new = '''
input CreateUserInput {
  name: String!
  email: String!
}
'''
    changes = diff_graphql(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "field_made_required"
    assert changes[0].field_name == "email"
    print("✅ test_field_made_required passed")


def test_output_field_made_non_null_is_safe():
    # OUTPUT field nullable -> non-null: readers already handle a value.
    # This was reported as field_made_required ("supply a value") before.
    old = "type User {\n  id: ID!\n  email: String\n}\n"
    new = "type User {\n  id: ID!\n  email: String!\n}\n"
    assert diff_graphql(old, new) == []
    print("✅ test_output_field_made_non_null_is_safe passed")


def test_type_removed():
    old = '''
type User {
  id: ID!
}
type Post {
  id: ID!
}
'''
    new = '''
type User {
  id: ID!
}
'''
    changes = diff_graphql(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "type_removed"
    assert changes[0].field_name == "Post"
    print("✅ test_type_removed passed")


def test_required_argument_added():
    old = '''
type Query {
  users(limit: Int): [User!]!
}
'''
    new = '''
type Query {
  users(limit: Int, offset: Int!): [User!]!
}
'''
    changes = diff_graphql(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "required_argument_added"
    assert "offset" in changes[0].field_name
    print("✅ test_required_argument_added passed")


def test_enum_value_removed():
    old = '''
enum Role {
  ADMIN
  USER
  MODERATOR
}
'''
    new = '''
enum Role {
  ADMIN
  USER
}
'''
    changes = diff_graphql(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "enum_value_removed"
    assert changes[0].field_name == "MODERATOR"
    print("✅ test_enum_value_removed passed")


def test_no_breaking_changes():
    old = '''
type User {
  id: ID!
  name: String!
}
'''
    new = '''
type User {
  id: ID!
  name: String!
  avatar: String
}
'''
    changes = diff_graphql(old, new)
    assert len(changes) == 0
    print("✅ test_no_breaking_changes (added nullable field is safe) passed")


def test_input_type_changes():
    old = '''
input CreateUserInput {
  name: String!
  email: String!
}
'''
    new = '''
input CreateUserInput {
  name: String!
  email: String!
  country: String!
}
'''
    changes = diff_graphql(old, new)
    # A new non-null input field with no default breaks every existing request.
    assert [(c.change_type, c.method, c.field_name) for c in changes] == [
        ("required_field_added", "CreateUserInput", "country")]
    # ...but not when it has a default, or is nullable.
    assert diff_graphql(old, new.replace("country: String!", 'country: String! = "US"')) == []
    assert diff_graphql(old, new.replace("country: String!", "country: String")) == []
    print("✅ test_input_type_changes passed (new required input field)")


def _one(old, new):
    changes = diff_graphql(old, new)
    assert len(changes) == 1, [(c.change_type, c.field_name) for c in changes]
    return changes[0]


def test_output_field_type_changed():
    for o, n in [("Int", "String"), ("ID!", "Int!"), ("[Post!]!", "Post!"),
                 ("Post", "[Post]"), ("[[Int]]", "[Int]"), ("[A]", "A")]:
        c = _one(f"type User {{\n  x: {o}\n}}\n", f"type User {{\n  x: {n}\n}}\n")
        assert c.change_type == "field_type_changed", (o, n, c.change_type)
        assert (c.old_type, c.new_type) == (o, n)
        assert c.method == "User" and c.field_name == "x"
    print("✅ test_output_field_type_changed passed")


def test_output_field_made_nullable_is_breaking():
    # Top level and inside a list: a reader that assumed a value breaks.
    for o, n in [("String!", "String"), ("[Post!]!", "[Post]!")]:
        c = _one(f"type User {{\n  x: {o}\n}}\n", f"type User {{\n  x: {n}\n}}\n")
        assert c.change_type == "field_type_changed"
        assert "nullable" in c.description
    # The same change on an INPUT field is safe: clients may now omit it.
    assert diff_graphql("input I {\n  x: String!\n}\n", "input I {\n  x: String\n}\n") == []
    print("✅ test_output_field_made_nullable_is_breaking passed")


def test_input_field_type_changed():
    c = _one("input I {\n  age: Int\n}\n", "input I {\n  age: String\n}\n")
    assert c.change_type == "field_type_changed"
    # inner list element made non-null: clients sending [null] are rejected
    c = _one("input I {\n  ids: [ID]\n}\n", "input I {\n  ids: [ID!]\n}\n")
    assert c.change_type == "field_made_required"
    # Made non-null WITH a default: requests that omit it still validate.
    assert diff_graphql("input I {\n  x: String\n}\n",
                        'input I {\n  x: String! = "a"\n}\n') == []
    print("✅ test_input_field_type_changed passed")


def test_argument_type_changed():
    old = "type Query {\n  user(id: ID!, limit: Int): User\n}\n"
    c = _one(old, "type Query {\n  user(id: Int!, limit: Int): User\n}\n")
    assert (c.change_type, c.field_name) == ("field_type_changed", "user(id)")
    assert (c.old_type, c.new_type) == ("ID!", "Int!")
    # Existing argument made required.
    c = _one(old, "type Query {\n  user(id: ID!, limit: Int!): User\n}\n")
    assert (c.change_type, c.field_name) == ("required_argument_added", "user(limit)")
    # Made required WITH a default is still optional for callers.
    assert diff_graphql(old, "type Query {\n  user(id: ID!, limit: Int! = 10): User\n}\n") == []
    # Required -> optional is safe.
    assert diff_graphql(old, "type Query {\n  user(id: ID, limit: Int): User\n}\n") == []
    print("✅ test_argument_type_changed passed")


def test_type_change_ignores_formatting():
    # Whitespace, trailing commas, directives and defaults are not type changes.
    old = ("type Query {\n  user(id: ID!, n: Int = 5): User\n  name: String!\n"
           "  tags: [String!]!\n}\n")
    new = ('type Query {\n  user(id:ID! ,n: Int=5): User\n'
           '  name: String! @deprecated(reason: "x")\n  tags: [ String! ]!\n}\n')
    assert diff_graphql(old, new) == []
    print("✅ test_type_change_ignores_formatting passed")


def test_argument_removed():
    old = '''
type Query {
  users(limit: Int, offset: Int = 0, filter: UserFilter!): [User!]!
}
'''
    new = '''
type Query {
  users(filter: UserFilter!): [User!]!
}
'''
    changes = diff_graphql(old, new)
    removed = sorted(c.field_name for c in changes
                     if c.change_type == "argument_removed")
    # Optional, defaulted and required arguments all break when removed:
    # GraphQL rejects any query that still passes an unknown argument.
    assert removed == ["users(limit)", "users(offset)"], changes
    assert all(c.method == "Query" for c in changes)
    assert len(changes) == 2, changes


def test_argument_removed_multiline_and_interface():
    old = '''
interface Node {
  children(
    first: Int
    after: String
  ): [Node]
}
'''
    new = '''
interface Node {
  children(
    first: Int
  ): [Node]
}
'''
    changes = diff_graphql(old, new)
    assert [(c.change_type, c.field_name) for c in changes] == [
        ("argument_removed", "children(after)")], changes


def test_argument_kept_is_not_removed():
    s = 'type Query {\n  users(limit: Int): [User]\n}\n'
    reordered = 'type Query {\n  users(  limit : Int ): [User]\n}\n'
    assert diff_graphql(s, reordered) == []


def test_argument_removed_routes_to_judgment():
    from app.change_types import canonical_op, is_judgment
    # Must NOT fall to the suffix heuristic ("removed" -> remove_field),
    # which is MECHANICAL and would auto-delete lines.
    assert canonical_op("argument_removed") == "restrict_schema"
    assert is_judgment("argument_removed")


def test_root_field_removed_is_an_operation():
    old = "type Query {\n  orders: [String]\n  me: User\n}\ntype User {\n  id: ID\n  name: String\n}\n"
    new = "type Query {\n  me: User\n}\ntype User {\n  id: ID\n}\n"
    got = sorted((c.change_type, c.method, c.field_name) for c in diff_graphql(old, new))
    # The root field is an operation (judgment); a plain type's field is not.
    assert got == [("field_removed", "User", "name"),
                   ("operation_removed", "Query", "orders")], got
    from app.change_types import canonical_op
    assert canonical_op("operation_removed") == "remove_operation"


def test_schema_block_names_the_roots():
    old = ("schema { query: Root }\ntype Root {\n  orders: [String]\n}\n"
           "type Query {\n  legacy: Int\n}\n")
    new = "schema { query: Root }\ntype Root {\n  x: Int\n}\ntype Query {\n  y: Int\n}\n"
    got = {(c.method, c.change_type) for c in diff_graphql(old, new)}
    # With a schema block, `Query` is an ordinary type and `Root` is the root.
    assert got == {("Root", "operation_removed"), ("Query", "field_removed")}, got


def test_multiple_changes():
    old = '''
type User {
  id: ID!
  name: String!
  email: String
  age: Int
  role: Role
}

enum Role {
  ADMIN
  USER
  MODERATOR
  GUEST
}

type Query {
  user(id: ID!): User
  users: [User!]!
}
'''
    new = '''
type User {
  id: ID!
  name: String!
  email: String!
  country: String!
}

enum Role {
  ADMIN
  USER
}

type Query {
  user(id: ID!): User
  users(limit: Int!): [User!]!
}
'''
    changes = diff_graphql(old, new)
    
    # Should detect:
    # 1. email: String → String! on an output type: SAFE, not reported
    # 2. age removed
    # 3. role removed
    # 4. MODERATOR enum removed
    # 5. GUEST enum removed
    # 6. required arg 'limit' added to users
    assert len(changes) >= 4  # At minimum these should be caught
    
    types_found = set(c.change_type for c in changes)
    # email String -> String! is on an OUTPUT type: safe, not reported.
    assert "field_made_required" not in types_found
    assert "required_field_added" not in types_found  # User is not an input
    assert "field_removed" in types_found
    assert "enum_value_removed" in types_found
    assert "required_argument_added" in types_found
    
    print(f"✅ test_multiple_changes passed ({len(changes)} changes detected)")


if __name__ == "__main__":
    test_parse_graphql()
    test_field_removed()
    test_field_made_required()
    test_type_removed()
    test_required_argument_added()
    test_enum_value_removed()
    test_no_breaking_changes()
    test_input_type_changes()
    test_multiple_changes()
    test_output_field_made_non_null_is_safe()
    test_output_field_type_changed()
    test_output_field_made_nullable_is_breaking()
    test_input_field_type_changed()
    test_argument_type_changed()
    test_type_change_ignores_formatting()
    test_argument_removed()
    test_argument_removed_multiline_and_interface()
    test_argument_kept_is_not_removed()
    test_argument_removed_routes_to_judgment()
    test_root_field_removed_is_an_operation()
    test_schema_block_names_the_roots()
    print("\n🎉 All GraphQL tests passed!")
