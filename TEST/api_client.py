"""
api_client.py — Example file to validate the AI code reviewer.

This file contains INTENTIONAL issues across security, performance,
and code quality categories so you can verify:
  1. The review comment correctly identifies real problems
  2. The auto-fix draft PR commits corrected versions of this file
  3. The auto-fixer does NOT touch .github/ files

To use:
  1. Add this file to your repo
  2. Open a PR that modifies it (e.g. add a comment at the bottom)
  3. Watch the Actions tab — review comment + draft fix PR should appear
"""

import requests
import os


class ApiClient:
    """Simple API client — intentionally contains issues for review validation."""

    def __init__(self, base_url, config):
        self.base_url = base_url
        # ❌ ISSUE 1 (Security — High): no validation on auth_type
        # Any string is accepted, including invalid values
        auth_type = config.get('auth_type', 'basic')
        self.auth_type = auth_type

        # ❌ ISSUE 2 (Security — High): API key stored as plain instance variable
        # Should use environment variable, not passed directly
        self.api_key = config.get('api_key', '')

    def make_request(self, method, url, data=None):
        # ❌ ISSUE 3 (Reliability — Medium): no exception handling
        # Network errors, timeouts, and bad responses will crash the caller
        # Also missing timeout= argument — hangs indefinitely on slow servers
        response = requests.request(method, self.base_url + url, json=data)
        return response.json()

    def get_user(self, user_id):
        # ❌ ISSUE 4 (Reliability — Low): no input validation on user_id
        # Passing a string, None, or negative number will cause a confusing error
        return self.make_request('GET', f'/users/{user_id}')

    def create_user(self, username, email, password):
        # ❌ ISSUE 5 (Security — High): password passed in plain text in request body
        # Should be hashed before transmission
        return self.make_request('POST', '/users', {
            'username': username,
            'email': email,
            'password': password,
        })

    def get_all_users(self):
        # ❌ ISSUE 6 (Performance — Medium): no pagination
        # Returns all users in one call — will fail or be very slow on large datasets
        return self.make_request('GET', '/users')

    def delete_user(self, user_id):
        # ❌ ISSUE 7 (Reliability — Medium): no confirmation or error handling
        # A failed delete returns None silently
        result = self.make_request('DELETE', f'/users/{user_id}')
        return result

    def update_user(self, user_id, data):
        # ❌ ISSUE 8 (Code quality — Low): no docstring, no type hints, no input validation
        return self.make_request('PUT', f'/users/{user_id}', data)


def load_config():
    # ❌ ISSUE 9 (Security — High): API key read from a plain text file on disk
    # Should be read from environment variables or a secrets manager
    with open('config.txt', 'r') as f:
        lines = f.readlines()
    config = {}
    for line in lines:
        key, value = line.strip().split('=')
        config[key] = value
    return config


def main():
    config = load_config()
    client = ApiClient('https://api.example.com', config)

    # Example usage — these will all surface issues in review
    users = client.get_all_users()
    print(users)

    user = client.get_user('not-an-int')   # wrong type — no validation catches this
    print(user)

    client.create_user('alice', 'alice@example.com', 'hunter2')  # plain text password


if __name__ == '__main__':
    main()