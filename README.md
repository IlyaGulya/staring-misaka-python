# Staring Misaka: Telegram Spam Detection Bot

Staring Misaka is a Telegram bot designed to detect and manage spam in group chats using Claude AI. It provides automatic or admin-approved spam detection and user banning capabilities.

## Features

- Automatic spam detection using Claude AI
- New user tracking
- Automatic or admin-approved banning of spammers
- Admin controls for ban approval and settings management
- SQLite database for storing user and ban information
- Docker deployment support

## Prerequisites

- Python 3.9+
- Telegram Bot API Token
- Anthropic API Key (for Claude AI)
- SQLite

## Installation

1. Clone the repository:
   ```
   git clone https://github.com/IlyaGulya/staring-misaka-python.git
   cd staring-misaka-python
   ```

2. Install the required dependencies:
   ```
   pip install -e .
   ```

3. Copy the `.env.example` file to `.env` and fill in the required values:
   ```
   cp .env.example .env
   ```

4. Edit the `.env` file with your specific configuration.

## Configuration

The bot uses environment variables for configuration. See the `.env.example` file for required variables.

## Usage

To run the bot locally:

```
python main.py
```

### Admin Commands

- `/toggle_approval`: Toggle whether admin approval is required for banning users
- `/status`: Check the current status of admin approval requirement

## Docker Deployment

1. Make sure you have Docker and Docker Compose installed.

2. Build and run the Docker container:
   ```
   docker-compose up -d
   ```

For Portainer deployment, use the `docker-compose.portainer.yml` file.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.