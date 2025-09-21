#!/bin/bash
set -e

# Docker entrypoint script for Staring Misaka bot
# Handles database migration before starting the application

echo "🚀 Starting Staring Misaka bot initialization..."

# Set production environment
export ENVIRONMENT=production

# Verify required environment variables
if [ -z "$DB_PATH" ]; then
    echo "❌ Error: DB_PATH environment variable is required in production"
    exit 1
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "❌ Error: ANTHROPIC_API_KEY environment variable is required"
    exit 1
fi

if [ -z "$API_ID" ] || [ -z "$API_HASH" ] || [ -z "$BOT_TOKEN" ]; then
    echo "❌ Error: Telegram API credentials (API_ID, API_HASH, BOT_TOKEN) are required"
    exit 1
fi

echo "✅ Environment variables validated"

# Function to check if database has our expected tables
check_database_schema() {
    cat > /tmp/check_schema.py << 'EOF'
import sqlite3
import sys
import os

try:
    db_path = os.environ.get('DB_PATH')
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    expected_tables = ['admin_settings', 'new_users', 'pending_ban_requests', 'banned_users']
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    missing_tables = [table for table in expected_tables if table not in existing_tables]
    if missing_tables:
        print('MISSING_TABLES: ' + str(missing_tables))
        sys.exit(1)
    else:
        print('SCHEMA_OK')
        
except Exception as e:
    print('ERROR: ' + str(e), file=sys.stderr)
    sys.exit(1)
EOF
    
    pixi run python /tmp/check_schema.py
    rm /tmp/check_schema.py
}

# Check if database file exists
if [ ! -f "$DB_PATH" ]; then
    echo "❌ Error: Database file not found at $DB_PATH"
    echo "   Please ensure the database is properly mounted or copied into the container"
    exit 1
fi

echo "📁 Database file found at $DB_PATH"

# Check database schema
echo "🔍 Checking database schema..."
SCHEMA_CHECK=$(check_database_schema)
if [[ $SCHEMA_CHECK == ERROR* ]]; then
    echo "❌ Database schema check failed: $SCHEMA_CHECK"
    exit 1
fi
echo "✅ Database schema looks good"

# Check if database is already stamped with Alembic version
echo "🔍 Checking Alembic migration status..."
CURRENT_VERSION=$(pixi run alembic current 2>/dev/null | grep -v "INFO" | head -1)

if [ -z "$CURRENT_VERSION" ]; then
    echo "⚠️  No Alembic version found - this appears to be a database created without Alembic"
    echo "📝 Stamping database with baseline migration..."
    
    # Stamp the database with the baseline migration
    # This tells Alembic that the database already has the baseline schema
    pixi run alembic stamp 7278023a76b5
    
    if [ $? -ne 0 ]; then
        echo "❌ Failed to stamp database with baseline migration"
        exit 1
    fi
    
    echo "✅ Database stamped with baseline migration"
else
    echo "✅ Database already has Alembic version: $CURRENT_VERSION"
fi

# Run any pending migrations
echo "🔄 Checking for pending migrations..."
pixi run alembic upgrade head

if [ $? -ne 0 ]; then
    echo "❌ Migration failed"
    exit 1
fi

echo "✅ Database migrations completed successfully"

# Show current migration status for logging
echo "📊 Current migration status:"
pixi run alembic current

# Check if we should skip starting the application
if [ "$SKIP_APP_START" = "true" ]; then
    echo "🧪 SKIP_APP_START=true - migration setup completed successfully!"
    echo "✅ All checks passed. Ready to start application."
    exit 0
fi

# Start the application
echo "🎯 Starting Staring Misaka bot..."
exec pixi run python main.py