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

Integration tests are located in `tests/integration/` directory. The project uses SQLAlchemy for database operations and Telethon for Telegram client functionality.

## Key Dependencies

- **telethon** - Telegram client library for both bot and userbot
- **anthropic** - Claude AI API client
- **instructor** - Structured output extraction from LLM responses
- **sqlalchemy** - Database ORM and session management
- **pydantic** - Data validation for LLM response models
