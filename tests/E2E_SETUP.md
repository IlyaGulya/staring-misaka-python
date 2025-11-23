# E2E Integration Test Setup Guide

This guide walks you through setting up end-to-end (e2e) integration tests for the Staring Misaka bot using Telegram's official test servers.

## Table of Contents

1. [Overview](#overview)
2. [Why Use Test Servers?](#why-use-test-servers)
3. [Prerequisites](#prerequisites)
4. [Step-by-Step Setup](#step-by-step-setup)
5. [Running E2E Tests](#running-e2e-tests)
6. [Troubleshooting](#troubleshooting)

## Overview

The E2E tests use **real Telegram clients** connected to **Telegram test servers** (`test.telegram.org`) to validate the complete bot workflow:

- ✅ Real MTProto protocol communication
- ✅ Real Telethon event handlers and message processing
- ✅ Real queue processor and database operations
- ✅ Isolated from production (test servers are completely separate)
- ✅ Mocked LLM calls (no actual Claude API costs)

## Why Use Test Servers?

Telegram provides official test servers specifically for development and testing:

- **Completely isolated** from production Telegram
- **Separate accounts** - your production account is never affected
- **Safe testing** of bans, deletions, and other destructive operations
- **Telegram-recommended** approach for bot development
- **No rate limits** (within reason)

## Prerequisites

Before you begin, you'll need:

1. **Python environment** with all project dependencies installed (`pixi install`)
2. **Telegram API credentials** from [my.telegram.org](https://my.telegram.org)
3. **Test server account** (phone number registered on test.telegram.org)
4. **Telegram client** that supports test servers (e.g., Telegram Desktop with test mode)

## Step-by-Step Setup

### Step 1: Get Telegram API Credentials

The session generation script in Step 2 will help you with this, or you can use existing credentials.

These credentials work for both production and test servers.

### Step 2: Generate StringSessions for Test Users

1. Download [Telegram Desktop](https://desktop.telegram.org/)
2. Close Telegram Desktop if running
3. Launch in test mode:
   - **On macOS:**
     ```bash
     /Applications/Telegram.app/Contents/MacOS/Telegram -testmode
     ```
   - **On Linux:**
     ```bash
     telegram-desktop -testmode
     ```
   - **On Windows:**
     ```bash
     telegram.exe -testmode
     ```
4. Log in with a **test phone number** (format: `99966XYYYY`)
   - Example: `9996612345` (DC 2)
   - Verification code will be `22222` (DC number repeated)
5. Complete the registration

**Note:** For E2E test automation, you don't need to manually create accounts - our script does it automatically (Step 3)!

### Step 3: Generate StringSessions for Test Users

You need StringSessions for two test accounts:

1. **TEST_USER_SESSION** - Regular user account for simulating spam/legit users
2. **TEST_ADMIN_SESSION** - Admin account that receives ban approval requests

#### Automatic Test Account Generation

The script **automatically generates test accounts** using Telegram's special test phone numbers - no need for a real phone or manual code entry!

```bash
# Set your API credentials
export TEST_API_ID=12345
export TEST_API_HASH=your_api_hash_here

# Run the generator script
pixi run python scripts/generate_test_session.py
```

The script will:
1. Ask you to choose a datacenter (DC 1, 2, or 3) - default is DC 2
2. **Automatically generate** a test phone number (format: `99966XYYYY`)
3. **Automatically verify** the account (no manual code entry needed!)
4. Create a StringSession for the test account
5. Print your User ID and StringSession

**Save the User ID and StringSession** - you'll need them for `.env.test`

### Step 4: Create and Configure a Test Bot

**REQUIRED**: E2E tests require a pre-created bot. You must create a bot before running tests.

1. **Run the setup script**:
   ```bash
   pixi run python scripts/setup_test_bot.py
   ```
   *Follow the interactive prompts.*

2. **Save the Token**:
   The script will output a `TEST_BOT_TOKEN`. You will need this for your `.env.test`.

*Note: The script automatically disables "Group Privacy" mode, which is required for the bot to see messages in groups.*

### Step 5: Configure Environment Variables

1. Copy the example file:
   ```bash
   cp .env.test.example .env.test
   ```

2. Edit `.env.test` and fill in your values:
   ```bash
   # From Step 1
   TEST_API_ID=12345
   TEST_API_HASH=your_api_hash_from_my_telegram_org

   # From Step 3
   TEST_USER_SESSION=1BVtsOHoBu7vO_J6qRYz...
   TEST_ADMIN_SESSION=1BVtsOHoBu7vO_J6qRYz...
   TEST_ADMIN_ID=987654321

   # From Step 4 (REQUIRED for E2E tests!)
   TEST_BOT_TOKEN=123456789:ABCDefGhiJklMnoPqrStuVwxYz
   ```

3. **Verify your `.gitignore`** includes `.env.test`:
   ```bash
   echo ".env.test" >> .gitignore
   ```

**Security Warning:** Never commit `.env.test` to git! It contains secrets.

## Running E2E Tests

Once setup is complete:

```bash
# Run all E2E tests
pixi run test-e2e

# Run specific test class
pixi run pytest tests/test_e2e_telegram.py::TestE2ESpamDetectionFlow -v
```
```

# Run specific test
pixi run pytest tests/test_e2e_telegram.py::TestE2ESpamDetectionFlow::test_complete_spam_detection_auto_ban -v

# Skip E2E tests in normal test runs
pixi run pytest tests/ -m "not e2e"
```

### What Gets Tested

1. **Complete Spam Detection Flow**
   - User joins → sends spam message → auto-banned → messages purged
   - Validates: NewUser tracking, MessageQueue, spam detection, ban execution

2. **Admin Approval Workflow**
   - Spam detected → admin notified → admin approves → user banned
   - Validates: PendingBanRequest, admin notifications, approval handling

3. **Auto-Approval for Legitimate Users**
   - User sends non-spam → auto-approved → removed from monitoring
   - Validates: ApprovedUser creation, monitoring removal

4. **Manual Ban Commands**
   - Admin uses `/sban user_id` or replies with `/sban`
   - Validates: Manual ban execution, message purging

## Troubleshooting

### Tests Skip with "Missing E2E env vars"

**Cause:** Environment variables not set correctly.

**Solution:**
```bash
# Check if .env.test exists
ls -la .env.test

# Verify variables are loaded
python -c "import os; print(os.getenv('TEST_API_ID'))"

# Ensure you're running with proper env loading
export $(cat .env.test | xargs)
pixi run test-e2e
```

### "TEST_USER_SESSION is not authorized"

**Cause:** StringSession is invalid or expired.

**Solution:**
1. Regenerate StringSession using `scripts/generate_test_session.py`
2. Make sure you're generating for TEST SERVERS (DC 2)
3. Update `.env.test` with new session

### "Bot has no public username"

**Cause:** Bot username not set.

**Solution:**
1. Connect to test servers
2. Find @BotFather
3. Send `/setusername` and choose your bot
4. Set a username (e.g., `my_test_spam_bot`)

### Connection Timeouts

**Cause:** Test servers might be slower or have connectivity issues.

**Solution:**
1. Increase timeouts in tests (already set to 20-30s)
2. Check if test servers are accessible:
   ```bash
   ping 149.154.167.40
   ```
3. Try running tests again

### Database Lock Errors

**Cause:** Multiple tests accessing same database.

**Solution:**
Tests use isolated temporary databases per test, but if issues persist:
1. Run tests sequentially: `pixi run pytest tests/test_e2e_telegram.py -v -n 0`
2. Check for leftover test processes: `ps aux | grep pytest`

### "User should be banned" Assertion Fails

**Cause:** Timing issues - ban hasn't been applied yet.

**Solution:**
Tests already use `wait_for_condition` with 10s timeout. If still failing:
1. Increase timeout in specific test
2. Check bot logs for errors during ban execution
3. Verify bot has admin rights in test group

## Best Practices

### Do's ✅

- ✅ Use test servers exclusively for E2E tests
- ✅ Keep `.env.test` in `.gitignore`
- ✅ Use throwaway test accounts (not personal accounts)
- ✅ Run E2E tests before major releases
- ✅ Clean up test data after each test
- ✅ Use separate test groups for different test scenarios

### Don'ts ❌

- ❌ Never use production credentials in E2E tests
- ❌ Never commit `.env.test` or StringSessions to git
- ❌ Don't use personal accounts for testing
- ❌ Don't run E2E tests in CI without secrets management
- ❌ Don't hardcode test credentials in test files

## Additional Resources

- [Telegram Test Servers Documentation](https://core.telegram.org/api/datacenter#switch-datacenter)
- [Telethon Documentation](https://docs.telethon.dev/)
- [Creating Bots Documentation](https://core.telegram.org/bots)

## Need Help?

If you're stuck, check:
1. This documentation
2. Test output and error messages
3. Project issues on GitHub
4. Telethon documentation

**Happy Testing! 🎉**
