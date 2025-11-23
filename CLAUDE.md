# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Staring Misaka is a Telegram spam detection bot that uses Claude AI to identify and manage spam in group chats. The bot consists of two main components: a regular Telegram bot for monitoring messages and admin interactions, and a userbot for executing ban commands.

## NOTES

- You MUST use escaped double quotes in the PYTHON CODE (AND ONLY INSIDE PYTHON CODE) when you run python code using pixi.
  WRONG: `pixi run python -c "print('hello')"`
  CORRECT: `pixi run python -c "print(\"hello\")"`
- You MUST NOT escape EXTERNAL DOUBLE QUOTES in environment variables

## Core Architecture

The application follows a modular structure with clear separation of concerns:

- **main.py** - Entry point that initializes and starts all components
- **telegram.py** - Main bot logic handling message monitoring, spam detection workflow, and admin commands
- **userbot.py** - Separate userbot client for executing ban commands via `/sban` 
- **llm.py** - Claude AI integration using the Instructor library for structured spam detection
- **db.py** - SQLAlchemy models and database session management
- **env.py** - Environment variable configuration loading

### Database Models

The bot uses SQLite with four main tables:
- `NewUser` - Tracks newly joined users who need monitoring
- `PendingBanRequest` - Stores admin approval requests for potential bans
- `BannedUser` - Records of banned users and their messages
- `AdminSettings` - Configuration for admin approval requirements

### Message Processing Flow

1. New users joining chats are tracked in `NewUser` table
2. Messages from new users are sent to Claude AI for spam detection
3. If spam is detected:
   - With admin approval enabled: Creates `PendingBanRequest` and notifies admin
   - Without admin approval: Automatically processes ban
4. Bans are executed via userbot using `/sban` command
5. User records are cleaned up and ban information is stored

## Development Commands

**IMPORTANT: Always use `pixi run` for all Python commands in this project.**

### Running the Application
```bash
pixi run python main.py
```

### Testing
```bash
pixi run test                    # Run all tests
pixi run test <path>             # Run specific test file/function
pixi run pytest <args>          # Run pytest with specific arguments
pixi run test-e2e               # Run E2E integration tests (requires test server credentials)
```

### Package Management (Pixi)
```bash
pixi install          # Install dependencies
pixi run <command>    # Run commands in pixi environment (ALWAYS USE THIS)
pixi add <package>    # Add new dependencies
```

### Other Commands
```bash
pixi run python <script>         # Run any Python script
pixi run <any-command>           # Run any command in pixi environment
```

### Docker Development
```bash
docker-compose up -d                    # Run with docker-compose
docker-compose -f docker-compose.portainer.yml up -d  # Run with Portainer config
```

## Configuration

The bot requires environment variables defined in `.env` file (see `.env.example`):
- Telegram API credentials (API_ID, API_HASH)
- Bot token and admin user ID
- Chat IDs to monitor (comma-separated)
- Anthropic API key for Claude AI
- Database and session file paths

## Testing

The project includes comprehensive testing at multiple levels:

### Unit and Integration Tests

Integration tests are located in `tests/` directory. The project uses SQLAlchemy for database operations and Telethon for Telegram client functionality. Most tests use mocks for Telegram clients and LLM calls.

```bash
pixi run test              # Run all tests (excluding E2E)
pixi run test-unit         # Run unit tests only
pixi run test-integration  # Run integration tests
pixi run test-coverage     # Run with coverage report
```

### End-to-End (E2E) Integration Tests

E2E tests validate the complete bot workflow using **real Telegram clients** connected to **Telegram test servers** (test.telegram.org). These tests provide the highest confidence that the bot works correctly in production-like conditions.

#### What E2E Tests Cover

1. **Complete Spam Detection Flow** - User joins → sends spam → auto-banned → messages purged
2. **Admin Approval Workflow** - Spam detected → admin notified → admin approves → user banned
3. **Auto-Approval for Legitimate Users** - Non-spam message → user auto-approved → monitoring removed
4. **Manual Ban Commands** - Admin uses `/sban` to ban users and purge messages

#### Running E2E Tests

E2E tests require special setup with Telegram test server credentials. They are marked with `@pytest.mark.e2e` and will skip gracefully if credentials are not available.

```bash
# Run E2E tests (requires .env.test with test server credentials)
pixi run test-e2e

# Skip E2E tests in normal test runs
pixi run pytest tests/ -m "not e2e"

# Run specific E2E test
pixi run pytest tests/test_e2e_telegram.py::TestE2ESpamDetectionFlow -v
```

#### E2E Test Setup

E2E tests require:
- Telegram test server credentials (API_ID, API_HASH)
- Test bot created via @BotFather on test servers
- Test user and admin accounts with StringSession
- Test group where bot has admin rights

**Detailed setup instructions:** See `tests/E2E_SETUP.md`

**Quick setup:**
1. Copy `.env.test.example` to `.env.test`
2. Get test API credentials from https://my.telegram.org
3. Connect to test servers using Telegram Desktop in test mode (`-testmode` flag)
4. Create a test bot via @BotFather on test servers
5. Generate StringSessions for test users:
   ```bash
   pixi run python scripts/generate_test_session.py
   ```
6. Create a test group and add your bot as admin
7. Fill in `.env.test` with your test credentials
8. Run: `pixi run test-e2e`

#### Why Use Test Servers?

- **Completely isolated** from production Telegram
- **Safe testing** of bans, deletions, and other destructive operations
- **Telegram-recommended** approach for bot testing
- **Real MTProto flow** - catches bugs that mocks miss

**Important:** Never use production credentials or real user accounts for E2E tests. Test servers are specifically designed for development and testing.

## Key Dependencies

- **telethon** - Telegram client library for both bot and userbot
- **anthropic** - Claude AI API client
- **instructor** - Structured output extraction from LLM responses
- **sqlalchemy** - Database ORM and session management
- **pydantic** - Data validation for LLM response models
