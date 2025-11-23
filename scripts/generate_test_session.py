#!/usr/bin/env python3
"""
Generate StringSession for Telegram Test Servers

This script creates a StringSession for use in E2E integration tests.
It connects to Telegram's Test Servers (DC 2 by default).

Usage:
    python scripts/generate_test_session.py

Requirements:
    - TEST_API_ID and TEST_API_HASH environment variables
    - Or provide them interactively when prompted

Note:
    You must provide a phone number. If using a real number, you will
    receive a confirmation code via SMS or the Telegram app (if logged
    into the Test Environment elsewhere).
"""

import os
import sys
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

# Add parent directory to path to import from root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from server_validation import validate_test_server_ip

# Load environment variables
load_dotenv(".env.test")

# Standard Telegram Test Server DCs
TEST_DCS = {
    1: ('149.154.175.10', 80),
    2: ('149.154.167.40', 80),
    3: ('149.154.175.117', 80),
}


async def generate_test_session():
    print("=" * 70)
    print("Telegram Test Server StringSession Generator")
    print("=" * 70)
    print()

    # 1. Get API Credentials
    api_id = os.getenv("TEST_API_ID")
    api_hash = os.getenv("TEST_API_HASH")

    if not api_id or not api_hash:
        print("TEST_API_ID and TEST_API_HASH not found in environment.")
        print("You can get these from https://my.telegram.org")

        try:
            api_id = int(input("Enter your API_ID: ").strip())
            api_hash = input("Enter your API_HASH: ").strip()
        except ValueError:
            print("Error: API_ID must be a number")
            sys.exit(1)
    else:
        api_id = int(api_id)

    # 2. Choose DC (from env or interactive)
    # Check if datacenter is configured in environment
    env_dc_ip = os.getenv("TEST_DATACENTER_IP")
    env_dc_port = os.getenv("TEST_DATACENTER_PORT", "80")
    env_dc_id = os.getenv("TEST_DC_ID")

    if env_dc_ip:
        # Use environment configuration
        dc_host = env_dc_ip
        dc_port = int(env_dc_port)
        dc_number = int(env_dc_id) if env_dc_id else 2
        print(f"\nUsing datacenter from environment: DC{dc_number} ({dc_host}:{dc_port})")
    else:
        # Interactive selection
        print("\nChoose a test datacenter (DC):")
        print("  1 - DC1 (149.154.175.10)")
        print("  2 - DC2 (149.154.167.40) - Recommended")
        print("  3 - DC3 (149.154.175.117)")

        try:
            dc_choice = input("Enter DC number (1-3, default 2): ").strip()
            dc_number = int(dc_choice) if dc_choice else 2
            if dc_number not in TEST_DCS:
                print(f"Invalid DC. Defaulting to DC2.")
                dc_number = 2
        except ValueError:
            print("Invalid input. Defaulting to DC2.")
            dc_number = 2

        dc_host, dc_port = TEST_DCS[dc_number]

    # KILL-SWITCH: Validate configured datacenter IP before connecting
    try:
        validate_test_server_ip(dc_host)
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)

    # 3. Get Phone Number
    print(f"\nConnecting to Test DC{dc_number}...")
    phone = input("Enter your phone number (e.g., +1234567890): ").strip()
    while not phone:
        phone = input("Phone number is required. Please enter it: ").strip()

    # Initialize Client
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        use_ipv6=False
    )

    # Force connection to specific Test DC
    client.session.set_dc(dc_number, dc_host, dc_port)

    try:
        print("Connecting...")
        await client.connect()

        # KILL-SWITCH: Verify we actually connected to a test server
        actual_ip = client.session.server_address
        try:
            validate_test_server_ip(actual_ip)
        except RuntimeError as e:
            print(str(e))
            await client.disconnect()
            sys.exit(1)
        print(f"✅ Connected securely to Test Server ({actual_ip})")

        if not await client.is_user_authorized():
            print(f"Requesting login code for {phone}...")
            await client.send_code_request(phone)

            code = input("Enter the code you received: ").strip()
            try:
                await client.sign_in(phone, code)
            except Exception as e:
                if 'PHONE_NUMBER_UNOCCUPIED' in str(e) or 'is not registered' in str(e).lower():
                    print("\nNumber not registered on Test Server.")
                    do_signup = input("Do you want to sign up? (y/n): ").lower()
                    if do_signup == 'y':
                        first_name = input("Enter first name: ").strip()
                        last_name = input("Enter last name (optional): ").strip()
                        await client.sign_up(code, first_name, last_name)
                    else:
                        raise
                else:
                    raise

        # Login Success
        me = await client.get_me()
        session_string = client.session.save()

        print("\n" + "=" * 70)
        print("✅ LOGIN SUCCESSFUL")
        print("=" * 70)
        print(f"User: {me.first_name} (ID: {me.id})")
        print(f"Phone: {me.phone}")
        print("-" * 70)
        print("Your StringSession (Save this to .env.test):")
        print("-" * 70)
        print(session_string)
        print("-" * 70)
        print(f"TEST_USER_SESSION={session_string}")
        print(f"TEST_ADMIN_SESSION={session_string}")
        print(f"TEST_ADMIN_ID={me.id}")
        print("-" * 70)

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(generate_test_session())
    except KeyboardInterrupt:
        print("\nCancelled.")