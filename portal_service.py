import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ["PORTAL_BASE_URL"]  # required, set in .env


class PortalService:
    def __init__(self, email: str = None, password: str = None):
        """Create a PortalService instance.
        
        Args:
            email: Portal email. Defaults to PORTAL_EMAIL from .env.
            password: Portal password. Defaults to PORTAL_PASSWORD from .env.
        """
        self.email = email or os.getenv("PORTAL_EMAIL")
        self.password = password or os.getenv("PORTAL_PASSWORD")
        self.access_token = None
        self.user_id = None
        self.tenant_id = None

    def login(self):
        """Login to the portal and store the access token."""
        url = f"{BASE_URL}/api/v1/auth/login"
        payload = {
            "email": self.email,
            "password": self.password,
            "rememberMe": True
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success"):
                self.access_token = data["data"]["access_token"]
                self.user_id = data["data"]["user"]["id"]
                self.tenant_id = data["data"]["user"]["tenant_id"]
                print("✅ Portal login successful")
                return True
            else:
                print(f"❌ Portal login failed: {data.get('message')}")
                return False
        except Exception as e:
            print(f"❌ Portal login error: {e}")
            return False

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

    def _request(self, method: str, url: str, **kwargs):
        """Helper to make requests with automatic retry on 401 Unauthorized."""
        if not self.access_token:
            self.login()
            
        kwargs["headers"] = self._headers()
        kwargs["timeout"] = kwargs.get("timeout", 15)
        
        resp = requests.request(method, url, **kwargs)
        
        # If token expired, login again and retry exactly once
        if resp.status_code == 401:
            print("⚠️ Portal token expired. Re-authenticating...")
            if self.login():
                kwargs["headers"] = self._headers()
                resp = requests.request(method, url, **kwargs)
                
        return resp

    def get_categories(self):
        """Fetch all daily activity categories."""
        url = f"{BASE_URL}/api/v1/global-codes/categories/DailyActivityCategory"
        try:
            resp = self._request("GET", url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success"):
                return data["data"]["items"]
            return []
        except Exception as e:
            print(f"❌ Error fetching categories: {e}")
            return []

    def get_projects(self):
        """Fetch all assigned projects."""
        url = f"{BASE_URL}/api/v1/daily-status/my-projects"
        try:
            resp = self._request("GET", url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success"):
                return data["data"]["items"]
            return []
        except Exception as e:
            print(f"❌ Error fetching projects: {e}")
            return []

    def get_todays_logged_minutes(self, activity_date: str = None) -> int:
        """Fetch how many minutes have already been logged for a given date (defaults to today)."""
        if not activity_date:
            activity_date = datetime.now().strftime("%Y-%m-%d")

        url = f"{BASE_URL}/api/v1/daily-status/my-day?date={activity_date}"
        try:
            resp = self._request("GET", url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success"):
                return data["data"].get("total_minutes", 0)
            return 0
        except Exception as e:
            print(f"❌ Error fetching todays logged minutes: {e}")
            return 0

    def submit_entry(self, category_id: str, duration_minutes: int, title: str, notes: str, project_id: str = None, activity_date: str = None):
        """Submit a single DSR time entry to the portal."""
        if not activity_date:
            activity_date = datetime.now().strftime("%Y-%m-%d")

        url = f"{BASE_URL}/api/v1/daily-status/entries"
        payload = {
            "activity_date": activity_date,
            "category_id": category_id,
            "duration_minutes": duration_minutes,
            "title": title,
            "notes": notes,
        }
        # Ignore literal "null", empty strings, or None
        if project_id and project_id.lower() != "null":
            payload["project_id"] = project_id

        # Log the full payload so we can debug any 400 errors
        print(f"📤 Submitting entry payload: {payload}")

        try:
            resp = self._request("POST", url, json=payload)

            # Always log the response body on non-2xx so we know exactly why it failed
            if not resp.ok:
                print(f"❌ Entry failed [{resp.status_code}]: {resp.text}")
                resp.raise_for_status()

            data = resp.json()
            if data.get("success"):
                print(f"✅ Entry submitted: {title} ({duration_minutes} min)")
                return True, data
            else:
                print(f"❌ Entry rejected by portal: {data.get('message')} | full: {data}")
                return False, data
        except requests.HTTPError as e:
            print(f"❌ HTTP error submitting entry: {e}")
            return False, str(e)
        except Exception as e:
            print(f"❌ Error submitting entry: {e}")
            return False, str(e)

    def submit_all_entries(self, tasks: list):
        """
        Submit multiple DSR entries.
        tasks: list of dicts with keys:
            - category_id (str)
            - duration_minutes (int)
            - title (str)
            - notes (str)
            - project_id (str, optional)
        Returns (success_count, fail_count)
        """
        success_count = 0
        fail_count = 0
        for task in tasks:
            ok, _ = self.submit_entry(
                category_id=task["category_id"],
                duration_minutes=task["duration_minutes"],
                title=task["title"],
                notes=task.get("notes", task["title"]),
                project_id=task.get("project_id"),
            )
            if ok:
                success_count += 1
            else:
                fail_count += 1
        return success_count, fail_count
