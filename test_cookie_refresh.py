import os
import sys
import json
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from xxt_notifier import XuexitongClient, load_config, save_config, logger

def test_cookie_refresh():
    config_path = BASE_DIR / "config.json"
    backup_path = BASE_DIR / "config.json.bak"
    
    # Restore from backup if it exists (e.g. from an interrupted run)
    if backup_path.exists():
        print("Found backup config.json.bak. Restoring original config.json first.")
        shutil.move(backup_path, config_path)
        
    if not config_path.exists():
        print("Error: config.json not found.")
        sys.exit(1)
        
    # Check if phone and password are configured
    config = load_config()
    if not config.get("phone") or not config.get("password"):
        print("Skip: phone and password are not configured in config.json. Skipping test.")
        return
        
    # Backup original config
    shutil.copy(config_path, backup_path)
    print("Backed up config.json.")
    
    try:
        # Load and corrupt cookies
        config = load_config()
        original_cookies = config.get("cookies", [])
        
        # Corrupt the cookies with invalid values
        config["cookies"] = [
            {
                "name": "p_auth_token",
                "value": "invalid_expired_token_signature_value",
                "domain": ".chaoxing.com",
                "path": "/"
            }
        ]
        save_config(config)
        print("Corrupted cookies in config.json.")
        
        # Initialize client and fetch courses
        client = XuexitongClient()
        courses = client.get_course_list()
        
        # Verify courses fetched
        if courses is not None:
            print(f"Success: Fetched {len(courses)} courses.")
        else:
            print("Failed: No courses fetched.")
            
        # Verify cookies updated in config.json
        updated_config = load_config()
        updated_cookies = updated_config.get("cookies", [])
        
        # Check if the cookies changed from our corrupted value
        has_refreshed = False
        for c in updated_cookies:
            if c["name"] == "p_auth_token" and c["value"] != "invalid_expired_token_signature_value":
                has_refreshed = True
                break
                
        if has_refreshed:
            print("Success: Cookies were auto-refreshed and saved to config.json.")
        else:
            print("Failed: Cookies were NOT auto-refreshed in config.json.")
            
        assert isinstance(courses, list), "Should successfully fetch courses after refresh (should return a list)"
        assert has_refreshed, "Cookies should be updated in config.json"
        
        print("ALL TESTS PASSED!")
        
    finally:
        # Restore backup
        if backup_path.exists():
            shutil.move(backup_path, config_path)
            print("Restored original config.json.")

if __name__ == "__main__":
    test_cookie_refresh()
