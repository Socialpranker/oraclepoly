#!/bin/bash
# Create a new timestamped migration file.
#
# Usage:
#   ./scripts/create_migration.sh add_user_preferences
#
# Creates: migrations/20260319223000_add_user_preferences.sql

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <migration_name>"
    echo "Example: $0 add_user_preferences"
    exit 1
fi

NAME="$1"
TIMESTAMP=$(date +"%Y%m%d%H%M%S")
FILENAME="migrations/${TIMESTAMP}_${NAME}.sql"

cat > "$FILENAME" << EOF
-- ${TIMESTAMP}_${NAME}.sql
-- Description: TODO

-- Write your migration SQL here.

EOF

echo "✓ Created: $FILENAME"
