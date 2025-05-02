# password_manager.py

import json
import hashlib
import subprocess
import logging
import os

class PasswordManager:
    def __init__(self, credentials_file='/etc/necris/credentials.json'):
        self.credentials_file = credentials_file
        self.logger = logging.getLogger(__name__)
        self.password_file = '/etc/necris/smb.secret'
        
        # Ensure credentials directory exists
        os.makedirs(os.path.dirname(credentials_file), exist_ok=True)
        
        # Initialize default credentials if they don't exist
        if not os.path.exists(credentials_file):
            self.init_credentials()
    
    def init_credentials(self):
        """Initialize default credentials"""
        credentials = {
            'username': 'necris-client',
            'password': hashlib.sha256('necris-is-awesome'.encode()).hexdigest(),
            'is_default_password': True  # Add this flag
        }
        self._save_credentials(credentials)
        self._update_samba_password('necris-is-awesome')
    
    def _save_credentials(self, credentials):
        """Save credentials to file with secure permissions"""
        with open(self.credentials_file, 'w') as f:
            json.dump(credentials, f)
        os.chmod(self.credentials_file, 0o600)
    
    def get_credentials(self):
        """Get current credentials"""
        with open(self.credentials_file, 'r') as f:
            return json.load(f)
    
    def _update_samba_password(self, new_password):
        """Update Samba user password"""
        try:
            proc = subprocess.Popen(
                ['smbpasswd', 'necris-client'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            # Send password twice (for confirmation)
            proc.communicate(input=f"{new_password}\n{new_password}\n".encode())
            
            if proc.returncode == 0:
                self.logger.info("Successfully updated Samba password")
                return True
            else:
                self.logger.error("Failed to update Samba password")
                return False
                
        except Exception as e:
            self.logger.error(f"Error updating Samba password: {e}")
            return False
    
    def get_current_password(self):
        """Get the actual password (for Samba setup)"""
        credentials = self.get_credentials()
        if credentials.get('is_default_password', True):
            return 'necris-is-awesome'
        else:
            # We'll need to store the actual password in a secure location
            try:
                with open(self.password_file, 'r') as f:
                    return f.read().strip()
            except:
                return 'necris-is-awesome'
    
    def _terminate_smb_sessions(self):
        """Force disconnect all SMB sessions for necris-client user"""
        try:
            # Get all sessions in JSON format
            result = subprocess.run(
                ['smbstatus', '--json'],
                capture_output=True,
                text=True,
                check=True
            )
            
            if result.returncode == 0:
                try:
                    status = json.loads(result.stdout)
                    
                    # Process sessions
                    if 'sessions' in status:
                        for session in status['sessions']:
                            if session.get('username') == 'necris-client':
                                # Get process ID for this session
                                pid = session.get('pid')
                                if pid:
                                    # Terminate specific session
                                    subprocess.run(['kill', '-TERM', str(pid)])
                                    self.logger.info(f"Terminated SMB session PID: {pid}")
                    
                    # Also process encrypted sessions if present
                    if 'encrypted_sessions' in status:
                        for session in status['encrypted_sessions']:
                            if session.get('username') == 'necris-client':
                                pid = session.get('pid')
                                if pid:
                                    subprocess.run(['kill', '-TERM', str(pid)])
                                    self.logger.info(f"Terminated encrypted SMB session PID: {pid}")
                    
                except json.JSONDecodeError:
                    self.logger.warning("Could not parse smbstatus JSON output, falling back to pkill")
                    subprocess.run(['pkill', '-SIGTERM', '-u', 'necris-client', 'smbd'])
            else:
                # Fallback if smbstatus fails
                self.logger.warning("smbstatus failed, falling back to pkill")
                subprocess.run(['pkill', '-SIGTERM', '-u', 'necris-client', 'smbd'])
            
            # Additional cleanup
            subprocess.run(['smbcontrol', 'smbd', 'close-share', '*'])
            
            self.logger.info("Successfully terminated all SMB sessions")
            return True
            
        except Exception as e:
            self.logger.error(f"Error terminating SMB sessions: {e}")
            return False

    def update_password(self, new_password):
        """Update password and terminate existing sessions"""
        try:
            # First terminate all existing sessions
            self._terminate_smb_sessions()
            
            # Then update Samba password
            if self._update_samba_password(new_password):
                # Update credentials file
                credentials = self.get_credentials()
                credentials['password'] = hashlib.sha256(new_password.encode()).hexdigest()
                credentials['is_default_password'] = False
                self._save_credentials(credentials)
                
                # Store actual password securely
                password_file = '/etc/necris/smb.secret'
                with open(password_file, 'w') as f:
                    f.write(new_password)
                os.chmod(password_file, 0o600)
                
                # Restart Samba services to ensure clean state
                subprocess.run(['systemctl', 'restart', 'smbd'], check=True)
                
                return True
            return False
            
        except Exception as e:
            self.logger.error(f"Error updating password: {e}")
            return False
    
    def verify_password(self, password):
        """Verify if a password matches stored credentials"""
        credentials = self.get_credentials()
        return hashlib.sha256(password.encode()).hexdigest() == credentials['password']