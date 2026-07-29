# mobile-backend/services/user_service.py
# Consumer of POST /users API

import requests

BASE_URL = "https://api.example.com"


def create_user(name: str, email: str, age: int = None) -> dict:
    """Create a new user via the Users API."""
    payload = {
        "name": name,
        "email": email,
    }
    if age is not None:
        payload["age"] = age

    response = requests.post(f"{BASE_URL}/users", json=payload)
    response.raise_for_status()
    return response.json()


def get_user(user_id: str) -> dict:
    """Get user by ID."""
    response = requests.get(f"{BASE_URL}/users/{user_id}")
    response.raise_for_status()
    return response.json()
