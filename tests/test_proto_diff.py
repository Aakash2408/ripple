"""
Tests for Protobuf diff engine.
"""
import sys
sys.path.insert(0, '.')

from app.proto_diff import diff_proto, parse_proto, format_proto_changes


def test_parse_proto():
    content = '''
syntax = "proto3";

message User {
  string id = 1;
  string name = 2;
  string email = 3;
  optional int32 age = 4;
}

message Address {
  string street = 1;
  string city = 2;
  required string country = 3;
}
'''
    messages = parse_proto(content)
    assert "User" in messages
    assert "Address" in messages
    assert len(messages["User"].fields) == 4
    assert messages["User"].fields["id"].number == 1
    assert messages["User"].fields["id"].type == "string"
    assert messages["Address"].fields["country"].label == "required"
    print("✅ test_parse_proto passed")


def test_field_removed():
    old = '''
message Payment {
  string id = 1;
  int64 amount = 2;
  string currency = 3;
  string description = 4;
}
'''
    new = '''
message Payment {
  string id = 1;
  int64 amount = 2;
  string currency = 3;
}
'''
    changes = diff_proto(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "field_removed"
    assert changes[0].field_name == "description"
    print("✅ test_field_removed passed")


def test_field_type_changed():
    old = '''
message Order {
  string id = 1;
  int64 amount = 2;
}
'''
    new = '''
message Order {
  string id = 1;
  string amount = 2;
}
'''
    changes = diff_proto(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "field_type_changed"
    assert "int64" in changes[0].field_type
    assert "string" in changes[0].field_type
    print("✅ test_field_type_changed passed")


def test_field_number_changed():
    old = '''
message Event {
  string id = 1;
  string type = 2;
}
'''
    new = '''
message Event {
  string id = 1;
  string type = 5;
}
'''
    changes = diff_proto(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "field_number_changed"
    print("✅ test_field_number_changed passed")


def test_message_removed():
    old = '''
message Foo {
  string id = 1;
}
message Bar {
  string name = 1;
}
'''
    new = '''
message Foo {
  string id = 1;
}
'''
    changes = diff_proto(old, new)
    assert any(c.change_type == "message_removed" and c.field_name == "Bar" for c in changes)
    print("✅ test_message_removed passed")


def test_required_field_added():
    old = '''
message Request {
  string id = 1;
  string name = 2;
}
'''
    new = '''
message Request {
  string id = 1;
  string name = 2;
  required string token = 3;
}
'''
    changes = diff_proto(old, new)
    assert len(changes) == 1
    assert changes[0].change_type == "required_field_added"
    assert changes[0].field_name == "token"
    print("✅ test_required_field_added passed")


def test_no_breaking_changes():
    old = '''
message User {
  string id = 1;
  string name = 2;
}
'''
    new = '''
message User {
  string id = 1;
  string name = 2;
  optional string avatar = 3;
}
'''
    changes = diff_proto(old, new)
    assert len(changes) == 0
    print("✅ test_no_breaking_changes passed")


def test_message_renamed():
    old = '''
message Refund {
  string id = 1;
  string payment_id = 2;
  int64 amount = 3;
}
'''
    new = '''
message PaymentRefund {
  string id = 1;
  string payment_id = 2;
  int64 amount = 3;
  string reason = 4;
}
'''
    changes = diff_proto(old, new)
    has_rename = any(c.change_type == "message_renamed" for c in changes)
    has_removed = any(c.change_type == "message_removed" for c in changes)
    assert has_rename or has_removed
    print("✅ test_message_renamed passed")


if __name__ == "__main__":
    test_parse_proto()
    test_field_removed()
    test_field_type_changed()
    test_field_number_changed()
    test_message_removed()
    test_required_field_added()
    test_no_breaking_changes()
    test_message_renamed()
    print("\n🎉 All proto tests passed!")
