# !/usr/bin/env python3
"""
Setup Test Bot on Telegram Test Servers

This script automates the creation and configuration of a bot on Telegram's
Test Servers (DC 2). It performs the following:
1. Authenticates using your Admin StringSession.
2. Interacts with @BotFather to create a new bot.
3. Automatically disables 'Group Privacy' mode (crucial for tests).
4. Outputs the BOT_TOKEN to use in .env.test.

Usage:
    python scripts/setup_test_bot.py

Requirements:
    - .env.test file with TEST_API_ID, TEST_API_HASH, TEST_ADMIN_SESSION
    - OR environment variables set manually
"""

import os
import sys
import asyncio
import re
import uuid
import argparse
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

# Add parent directory to path to import from root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from server_validation import validate_test_server_ip

# Load .env.test if it exists
load_dotenv(".env.test")


async def configure_bot_privacy(client, bot_username, max_retries=2):
    """Configure bot privacy settings (disable privacy mode).

    Uses /setprivacy command which accepts text-based bot selection.
    Returns True if successful, False otherwise.
    """
    for attempt in range(max_retries):
        try:
            print(f"\n{'Retrying p' if attempt > 0 else 'P'}rivacy configuration (attempt {attempt + 1}/{max_retries})...")

            async with client.conversation("@BotFather", timeout=30) as conv:
                # Clear any pending messages
                try:
                    while True:
                        await conv.get_response(timeout=0.3)
                except:
                    pass

                # Start fresh
                await conv.send_message("/cancel")
                resp = await conv.get_response()
                await asyncio.sleep(0.3)

                # Use /setprivacy command
                print(f"  Sending /setprivacy...")
                await conv.send_message("/setprivacy")
                resp = await conv.get_response()
                print(f"  Response: {resp.text[:100]}...")

                if "choose a bot" not in resp.text.lower():
                    print(f"  ❌ Unexpected /setprivacy response")
                    continue

                # Select the bot by username
                await asyncio.sleep(0.3)
                print(f"  Selecting bot @{bot_username}...")
                await conv.send_message(f"@{bot_username}")
                resp = await conv.get_response()
                print(f"  Response: {resp.text[:150]}...")

                # Check current status
                if "current status is: disabled" in resp.text.lower():
                    print("  ℹ️  Privacy mode is already disabled.")
                    return True

                if "current status is: enabled" in resp.text.lower():
                    # Disable privacy mode
                    await asyncio.sleep(0.3)
                    print(f"  Sending 'Disable'...")
                    await conv.send_message("Disable")
                    resp = await conv.get_response()
                    print(f"  Response: {resp.text[:100]}...")

                    if "success" in resp.text.lower() and "disabled" in resp.text.lower():
                        print("  ✅ Privacy mode disabled successfully.")
                        return True
                    else:
                        print(f"  ⚠️ Could not verify privacy mode was disabled")
                        continue

                if "help" in resp.text.lower() or "invalid" in resp.text.lower():
                    print(f"  ❌ Bot selection failed or not found")
                    continue

                print(f"  ⚠️ Unexpected response flow")

        except Exception as e:
            print(f"  ⚠️ Error during privacy configuration: {e}")
            import traceback
            traceback.print_exc()

        # Wait before retry
        if attempt < max_retries - 1:
            await asyncio.sleep(2)

    return False


async def setup_bot(skip_privacy=False):
    print("=" * 70)
    print("Telegram Test Server Bot Setup")
    print("=" * 70)
    print()

    # 1. Get Credentials
    api_id = os.getenv("TEST_API_ID")
    api_hash = os.getenv("TEST_API_HASH")
    admin_session = os.getenv("TEST_ADMIN_SESSION")

    # Get configured datacenter (for validation)
    expected_dc_ip = os.getenv("TEST_DATACENTER_IP", "149.154.167.40")

    if not all([api_id, api_hash, admin_session]):
        print("❌ Error: Missing credentials.")
        print("Please ensure TEST_API_ID, TEST_API_HASH, and TEST_ADMIN_SESSION")
        print("are set in your environment or .env.test file.")
        sys.exit(1)

    try:
        api_id = int(api_id)
    except ValueError:
        print("❌ Error: TEST_API_ID must be an integer.")
        sys.exit(1)

    print(f"Connecting to Telegram Test Servers ({expected_dc_ip})...")

    # Connect using the admin session
    client = TelegramClient(
        StringSession(admin_session),
        api_id,
        api_hash,
        use_ipv6=False
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print("❌ Error: Session is not authorized. Please run generate_test_session.py first.")
            sys.exit(1)

        # KILL-SWITCH: Validate configured datacenter IP
        try:
            validate_test_server_ip(expected_dc_ip)
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)

        # KILL-SWITCH: Verify we are actually on a test server
        server_ip = client.session.server_address
        try:
            validate_test_server_ip(server_ip)
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)

        print(f"✅ Connected securely to Test Server ({server_ip}).")

        # 2. Create Bot
        bot_suffix = uuid.uuid4().hex[:8]
        bot_username = f"misaka_test_{bot_suffix}_bot"
        bot_name = f"Misaka E2E {bot_suffix}"

        print(f"\nCreating bot: @{bot_username} ...")

        async with client.conversation("@BotFather", timeout=30) as conv:
            # Reset state
            await conv.send_message("/cancel")
            try:
                await conv.get_response(timeout=0.5)
            except:
                pass

            # Start creation
            await asyncio.sleep(0.3)  # Brief delay before starting
            await conv.send_message("/newbot")
            resp = await conv.get_response()

            if "Alright, a new bot" not in resp.text:
                print(f"❌ Failed to start bot creation. BotFather said: {resp.text}")
                return

            await asyncio.sleep(0.3)  # Brief delay between commands
            await conv.send_message(bot_name)
            resp = await conv.get_response()

            await asyncio.sleep(0.3)
            await conv.send_message(bot_username)
            resp = await conv.get_response()

            if "Sorry" in resp.text:
                print(f"❌ Failed to set username. BotFather said: {resp.text}")
                return

            # Extract token
            match = re.search(r'Use this token to access the HTTP API:\s+[`]?([0-9]+:[a-zA-Z0-9_-]+)[`]?', resp.text)
            if not match:
                print("❌ Could not extract token from BotFather response.")
                print(f"Response: {resp.text}")
                return

            token = match.group(1)
            print(f"✅ Bot created! Token: {token}")

        # 3. Configure Privacy (in separate conversation to avoid state issues)
        if not skip_privacy:
            print("\nConfiguring bot privacy...")
            # Wait a bit to let BotFather process the bot creation
            await asyncio.sleep(2)

            privacy_success = await configure_bot_privacy(client, bot_username)

            if not privacy_success:
                print("\n⚠️  WARNING: Privacy mode configuration failed.")
                print("You can manually disable it:")
                print("  1. Open @BotFather in Telegram")
                print("  2. Send /mybots")
                print(f"  3. Select @{bot_username}")
                print("  4. Click 'Bot Settings' → 'Group Privacy' → 'Turn off'")
        else:
            print("\n⚠️  Skipping privacy configuration (--skip-privacy flag set).")

        print("\n" + "=" * 70)
        print("SETUP COMPLETE")
        print("=" * 70)
        print(f"\nAdd this to your .env.test file:\n")
        print(f"TEST_BOT_TOKEN={token}")
        print("\n" + "=" * 70)

    except Exception as e:
        print(f"\n❌ An error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.disconnect()


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Create and configure a test bot on Telegram test servers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create bot with privacy mode configuration
  python scripts/setup_test_bot.py

  # Create bot without privacy configuration (manual setup required)
  python scripts/setup_test_bot.py --skip-privacy

Note: Privacy mode should be disabled for bots in groups. If automatic
configuration fails, you can disable it manually via @BotFather.
        """
    )
    parser.add_argument(
        '--skip-privacy',
        action='store_true',
        help='Skip automatic privacy mode configuration (you will need to disable it manually)'
    )

    args = parser.parse_args()

    try:
        asyncio.run(setup_bot(skip_privacy=args.skip_privacy))
    except KeyboardInterrupt:
        print("\nCancelled.")


if __name__ == "__main__":
    main()