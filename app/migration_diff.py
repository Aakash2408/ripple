from __future__ import annotations
"""
ripple/app/migration_diff.py

Database Migration Diff Engine — detects breaking changes in SQL schemas.

Breaking changes in database schemas:
- Column removed (SELECT * breaks, ORM models break)
- Column type changed (casting failures)
- Column made NOT NULL without default (existing NULLs fail)
- Table renamed (all queries break)
- Table removed (all references break)
- Index removed (performance regression, may break unique constraints)

Supports: raw SQL migrations, Prisma schema, Sequelize models, SQLAlchemy models.
V0: SQL DDL parsing (CREATE TABLE, ALTER TABLE).
"""

import re
from dataclasses import dataclass
from typing import Optional

from .diff_engine import BreakingChange
from .schema_parse import strip_comments, extract_blocks


@dataclass
class Column:
    """A database column definition."""
    name: str
    type: str
    nullable: bool
    default: Optional[str]
    primary_key: bool


@dataclass
class Table:
    """A database table definition."""
    name: str
    columns: dict[str, Column]


def parse_sql_schema(content: str) -> dict[str, Table]:
    """
    Parse SQL DDL (CREATE TABLE statements) into table definitions.
    Supports PostgreSQL, MySQL, SQLite syntax.
    """
    tables = {}
    
    # Find CREATE TABLE blocks
    create_pattern = re.compile(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\']?(\w+)[`"\']?\s*\(([^;]+)\)',
        re.IGNORECASE | re.DOTALL
    )
    
    for match in create_pattern.finditer(content):
        table_name = match.group(1)
        body = match.group(2)
        
        columns = {}
        # Parse column definitions
        for line in body.split(','):
            line = line.strip()
            if not line:
                continue
            # Skip constraints (PRIMARY KEY, FOREIGN KEY, UNIQUE, INDEX, CHECK)
            if re.match(r'^\s*(PRIMARY|FOREIGN|UNIQUE|INDEX|CHECK|CONSTRAINT)', line, re.IGNORECASE):
                continue
            
            col_match = re.match(
                r'[`"\']?(\w+)[`"\']?\s+(\w+(?:\(\d+(?:,\s*\d+)?\))?)',
                line, re.IGNORECASE
            )
            if col_match:
                col_name = col_match.group(1)
                col_type = col_match.group(2)
                nullable = 'NOT NULL' not in line.upper()
                default = None
                default_match = re.search(r'DEFAULT\s+(\S+)', line, re.IGNORECASE)
                if default_match:
                    default = default_match.group(1)
                primary_key = 'PRIMARY KEY' in line.upper()
                
                columns[col_name] = Column(
                    name=col_name, type=col_type,
                    nullable=nullable, default=default,
                    primary_key=primary_key
                )
        
        if columns:
            tables[table_name] = Table(name=table_name, columns=columns)
    
    return tables


def parse_prisma_schema(content: str) -> dict[str, Table]:
    """
    Parse Prisma schema file into table definitions.
    
    model User {
      id    String @id
      email String @unique
      name  String?
    }
    """
    tables = {}
    
    # schema_parse: brace-aware extraction plus comment stripping, matching
    # the other engines. Prisma models cannot nest today, so the old
    # r'\{([^}]+)\}' happened to work -- but it was the same latent hazard,
    # and consistency means one parsing primitive across all engines.
    clean = strip_comments(content, hash_comments=False)
    
    for model_name, body in extract_blocks(clean, 'model'):
        columns = {}
        for line in body.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('//') or line.startswith('@@'):
                continue
            
            parts = line.split()
            if len(parts) >= 2:
                col_name = parts[0]
                col_type = parts[1]
                nullable = col_type.endswith('?')
                col_type = col_type.rstrip('?')
                primary_key = '@id' in line
                default = None
                default_match = re.search(r'@default\(([^)]+)\)', line)
                if default_match:
                    default = default_match.group(1)
                
                columns[col_name] = Column(
                    name=col_name, type=col_type,
                    nullable=nullable, default=default,
                    primary_key=primary_key
                )
        
        if columns:
            tables[model_name] = Table(name=model_name, columns=columns)
    
    return tables


def diff_schema(old_content: str, new_content: str, file_path: str = "schema.sql") -> list[BreakingChange]:
    """
    Compare two database schemas and return breaking changes.
    Auto-detects format (SQL DDL or Prisma).
    """
    # Detect format
    if 'model ' in old_content and '{' in old_content:
        old_tables = parse_prisma_schema(old_content)
        new_tables = parse_prisma_schema(new_content)
    else:
        old_tables = parse_sql_schema(old_content)
        new_tables = parse_sql_schema(new_content)
    
    changes = []
    
    for table_name, old_table in old_tables.items():
        if table_name not in new_tables:
            changes.append(BreakingChange(
                change_type="table_removed",
                path=file_path,
                method=table_name,
                field_name=table_name,
                field_type="table",
                location="database",
                severity="breaking",
                description=f"Table '{table_name}' was removed. All queries, ORM models, and migrations referencing it will fail.",
            ))
            continue
        
        new_table = new_tables[table_name]
        
        # Check column removals
        for col_name, old_col in old_table.columns.items():
            if col_name not in new_table.columns:
                changes.append(BreakingChange(
                    change_type="column_removed",
                    path=file_path,
                    method=table_name,
                    field_name=col_name,
                    field_type=old_col.type,
                    location="database",
                    severity="breaking",
                    description=f"Column '{col_name}' removed from table '{table_name}'. Any SELECT, INSERT, or ORM model referencing it will break.",
                ))
            else:
                new_col = new_table.columns[col_name]
                
                # Type change
                if old_col.type.lower() != new_col.type.lower():
                    changes.append(BreakingChange(
                        change_type="column_type_changed",
                        path=file_path,
                        method=table_name,
                        field_name=col_name,
                        field_type=f"{old_col.type} → {new_col.type}",
                        old_type=old_col.type,
                        new_type=new_col.type,
                        location="database",
                        severity="breaking",
                        description=f"Column '{col_name}' in '{table_name}' changed type from '{old_col.type}' to '{new_col.type}'. Existing data may not cast correctly.",
                    ))
                
                # Made NOT NULL without default
                if old_col.nullable and not new_col.nullable and not new_col.default:
                    changes.append(BreakingChange(
                        change_type="column_made_not_null",
                        path=file_path,
                        method=table_name,
                        field_name=col_name,
                        field_type=new_col.type,
                        location="database",
                        severity="breaking",
                        description=f"Column '{col_name}' in '{table_name}' made NOT NULL without a default value. Existing rows with NULL will prevent migration.",
                    ))
        
        # Check for new NOT NULL columns without defaults
        for col_name, new_col in new_table.columns.items():
            if col_name not in old_table.columns:
                if not new_col.nullable and not new_col.default:
                    changes.append(BreakingChange(
                        change_type="not_null_column_added",
                        path=file_path,
                        method=table_name,
                        field_name=col_name,
                        field_type=new_col.type,
                        location="database",
                        severity="breaking",
                        description=f"New NOT NULL column '{col_name}' added to '{table_name}' without default. INSERT statements missing this column will fail.",
                    ))
    
    # Check for table renames (similar to proto message rename detection)
    for table_name in new_tables:
        if table_name not in old_tables:
            for old_name, old_table in old_tables.items():
                if old_name not in new_tables:
                    old_cols = set(old_table.columns.keys())
                    new_cols = set(new_tables[table_name].columns.keys())
                    overlap = len(old_cols & new_cols) / max(len(old_cols), 1)
                    if overlap > 0.7:
                        changes.append(BreakingChange(
                            change_type="table_renamed",
                            new_name=table_name,
                            path=file_path,
                            method=table_name,
                            field_name=f"{old_name} → {table_name}",
                            field_type="table",
                            location="database",
                            severity="breaking",
                            description=f"Table appears renamed from '{old_name}' to '{table_name}'. All queries and ORM models need updating.",
                        ))
                        break
    
    return changes


def format_migration_changes(changes: list[BreakingChange]) -> str:
    """Format database migration changes for display."""
    if not changes:
        return "✅ No breaking changes in database schema."
    
    lines = [f"⚠️  {len(changes)} breaking change(s) in database schema:", ""]
    for i, c in enumerate(changes, 1):
        lines.append(f"  [{i}] {c.change_type}")
        lines.append(f"      Table: {c.method}")
        lines.append(f"      Column: {c.field_name} ({c.field_type})")
        lines.append(f"      {c.description}")
        lines.append("")
    return "\n".join(lines)
