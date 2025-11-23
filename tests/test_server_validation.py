"""Test Server IP Validation - Test Module

This module re-exports the validate_test_server_ip function from the root
server_validation module for backward compatibility with existing imports.

The actual implementation is now at the project root to avoid layering violations.
"""

from server_validation import validate_test_server_ip

__all__ = ['validate_test_server_ip']
