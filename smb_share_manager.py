#!/usr/bin/env python3

import os
import subprocess
import logging
import time
import pwd
import grp
from pathlib import Path
import configparser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from password_manager import PasswordManager

class SMBShareManager:
    def __init__(self):
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            filename='/var/log/smb_share_manager.log'
        )
        self.logger = logging.getLogger(__name__)
        
        # Initialize password manager
        self.password_manager = PasswordManager()
        
        # Get default credentials
        credentials = self.password_manager.get_credentials()
        self.smb_user = credentials['username']
        
        # Set default system user (for file ownership)
        self.system_user = os.environ.get('SUDO_USER', 'necris-user')
        
        try:
            self.uid = pwd.getpwnam(self.system_user).pw_uid
            self.gid = grp.getgrnam(self.system_user).gr_gid
        except KeyError:
            self.logger.error(f"Could not find user {self.system_user}")
            raise SystemExit(f"User {self.system_user} not found in system")
            
        # Base mount directory
        self.mount_base = Path(f'/media/{self.system_user}')
        
        # Samba configuration
        self.smb_conf_path = '/etc/samba/smb.conf'
        self.shares_conf_path = '/etc/samba/shares.conf'
        self.active_shares = set()
        
        # Initialize
        self.setup_samba_user()
        self.setup_samba_config()
        self.verify_samba_setup()
        self.validate_existing_shares()
        
        # Setup filesystem watchdog
        self.observer = Observer()
        self.setup_watchdog()

    def setup_samba_user(self):
        """Create the default Samba user if it doesn't exist"""
        try:
            # First, create system user if it doesn't exist
            try:
                pwd.getpwnam(self.smb_user)
            except KeyError:
                # Create system user with home directory and shell
                subprocess.run([
                    'useradd',
                    '-m',  # Create home directory
                    '-s', '/bin/false',  # No shell access
                    self.smb_user
                ], check=True)
                self.logger.info(f"Created system user {self.smb_user}")

            # Check if Samba user exists
            result = subprocess.run(['pdbedit', '-L'], capture_output=True, text=True)
            
            if self.smb_user not in result.stdout:
                # Get current password from password manager
                current_password = self.password_manager.get_current_password()
                
                # Create new Samba user
                subprocess.run(['smbpasswd', '-a', self.smb_user], 
                            input=f"{current_password}\n{current_password}\n".encode(),
                            check=True)
                
                # Enable the user
                subprocess.run(['smbpasswd', '-e', self.smb_user], check=True)
                
                self.logger.info(f"Created and enabled Samba user {self.smb_user}")
            
            self.logger.info(f"Samba user {self.smb_user} setup completed")
            
        except Exception as e:
            self.logger.error(f"Failed to setup Samba user: {e}")
            raise


    def setup_samba_config(self):
        """Ensure Samba configuration is properly set up"""
        try:
            # Create shares.conf if it doesn't exist
            if not os.path.exists(self.shares_conf_path):
                with open(self.shares_conf_path, 'w') as f:
                    f.write('; USB Share configurations\n')
            
            # Check if main smb.conf includes our shares.conf
            include_line = f'include = {self.shares_conf_path}'
            
            # Create new smb.conf with proper settings
            smb_config = f'''[global]
        workgroup = WORKGROUP
        server string = Necris File Server
        server role = standalone server
        security = user
        map to guest = never
        encrypt passwords = yes
        
        # Protocol settings
        server min protocol = NT1
        client min protocol = NT1
        ntlm auth = yes
        lanman auth = yes
        
        # Apple device support
        vfs objects = fruit streams_xattr
        fruit:metadata = stream
        fruit:model = MacSamba
        fruit:posix_rename = yes
        fruit:veto_appledouble = no
        fruit:wipe_intentionally_left_blank_rfork = yes
        fruit:delete_empty_adfiles = yes
        
        # Logging
        log file = /var/log/samba/log.%m
        max log size = 1000
        logging = file
        panic action = /usr/share/samba/panic-action %d
        
        # Authentication
        passdb backend = tdbsam
        obey pam restrictions = yes
        unix password sync = yes
        passwd program = /usr/bin/passwd %u
        passwd chat = *Enter\\snew\\s*\\spassword:* %n\\n *Retype\\snew\\s*\\spassword:* %n\\n *password\\supdated\\ssuccessfully* .
        pam password change = yes
        
; USB Share configurations
{include_line}
'''
            # Write the configuration
            with open(self.smb_conf_path, 'w') as f:
                f.write(smb_config)
                
            # Ensure proper permissions
            os.chmod(self.smb_conf_path, 0o644)
            
            # Restart Samba services to apply changes
            subprocess.run(['systemctl', 'restart', 'smbd'], check=True)
            subprocess.run(['systemctl', 'restart', 'nmbd'], check=True)
            
            self.logger.info("Samba configuration setup completed")
            
        except Exception as e:
            self.logger.error(f"Failed to setup Samba configuration: {e}")
            raise

    def validate_existing_shares(self):
        """Check for and clean up stale shares"""
        self.logger.info("Validating existing Samba shares...")
        
        try:
            config = configparser.ConfigParser(strict=False)
            config.read(self.shares_conf_path)
            
            shares_to_remove = []
            
            for share_name in config.sections():
                if share_name.startswith('USB_'):
                    share_path = config[share_name].get('path')
                    
                    if not share_path or not os.path.exists(share_path) or not os.path.ismount(share_path):
                        shares_to_remove.append(share_name)
                        self.logger.warning(f"Found stale share: {share_name} for path {share_path}")
            
            # Remove stale shares
            if shares_to_remove:
                self.remove_shares(shares_to_remove)
                
            # Update active shares set
            self.update_active_shares()
            
        except Exception as e:
            self.logger.error(f"Error validating existing shares: {e}")


    def verify_samba_setup(self):
        """Verify Samba user setup"""
        try:
            # Check if user exists in Samba database
            result = subprocess.run(['pdbedit', '-L'], 
                                capture_output=True, 
                                text=True)
            
            if self.smb_user not in result.stdout:
                self.logger.warning(f"Samba user {self.smb_user} not found, recreating...")
                self.setup_samba_user()
                
            # Test user authentication with current password
            current_password = self.password_manager.get_current_password()
            test_auth = subprocess.run(
                ['smbclient', '-L', 'localhost', '-U', f'{self.smb_user}%{current_password}'],
                capture_output=True
            )
            
            if test_auth.returncode != 0:
                self.logger.warning("Samba authentication test failed, resetting user...")
                self.setup_samba_user()
                
            self.logger.info("Samba setup verification completed")
            
        except Exception as e:
            self.logger.error(f"Failed to verify Samba setup: {e}")
            raise


    def update_active_shares(self):
        """Update the set of currently active shares"""
        config = configparser.ConfigParser(strict=False)
        config.read(self.shares_conf_path)
        self.active_shares = {section for section in config.sections() if section.startswith('USB_')}

    def create_share(self, mount_point):
        """Create a new Samba share for a mounted device with user authentication"""
        try:
            device_name = mount_point.name
            share_name = f"USB_{device_name}"
            
            # Skip if share already exists
            if share_name in self.active_shares:
                self.logger.info(f"Share {share_name} already exists")
                return True
            
            config = configparser.ConfigParser(strict=False)
            config.read(self.shares_conf_path)
            
            # Share configuration with user authentication
            config[share_name] = {
                'comment': f'USB Drive {device_name}',
                'path': str(mount_point),
                'browseable': 'yes',
                'read only': 'no',
                'writable': 'yes',
                'guest ok': 'no',  # Disable guest access
                'valid users': self.smb_user,  # Only allow our default user
                'create mask': '0666',
                'directory mask': '0777',
                'force user': self.system_user,
                'force group': self.system_user
            }
            
            # Write configuration
            with open(self.shares_conf_path, 'w') as f:
                config.write(f)
            
            # Reload Samba configuration
            subprocess.run(['systemctl', 'reload', 'smbd'], check=True)
            
            # Update active shares
            self.active_shares.add(share_name)
            
            self.logger.info(f"Successfully created share {share_name} for {mount_point}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to create share for {mount_point}: {e}")
            return False


    def remove_shares(self, share_names):
        """Remove one or more Samba shares"""
        if not isinstance(share_names, list):
            share_names = [share_names]
            
        try:
            config = configparser.ConfigParser(strict=False)
            config.read(self.shares_conf_path)
            
            modified = False
            for share_name in share_names:
                if share_name in config.sections():
                    config.remove_section(share_name)
                    self.active_shares.discard(share_name)
                    modified = True
                    self.logger.info(f"Removed share {share_name}")
            
            if modified:
                # Write updated configuration
                with open(self.shares_conf_path, 'w') as f:
                    config.write(f)
                    
                # Reload Samba configuration
                subprocess.run(['systemctl', 'reload', 'smbd'], check=True)
                
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to remove shares {share_names}: {e}")
            return False

    class USBMountHandler(FileSystemEventHandler):
        def __init__(self, manager):
            self.manager = manager
            
        def on_created(self, event):
            if event.is_directory and str(event.src_path).startswith(str(self.manager.mount_base)):
                self.manager.logger.info(f"New mount point detected: {event.src_path}")
                # Small delay to ensure mount is complete
                time.sleep(1)
                if os.path.ismount(event.src_path):
                    self.manager.create_share(Path(event.src_path))
                    
        def on_deleted(self, event):
            if event.is_directory and str(event.src_path).startswith(str(self.manager.mount_base)):
                self.manager.logger.info(f"Mount point removed: {event.src_path}")
                share_name = f"USB_{os.path.basename(event.src_path)}"
                self.manager.remove_shares([share_name])

    def setup_watchdog(self):
        """Setup filesystem monitoring for mount points"""
        event_handler = self.USBMountHandler(self)
        self.observer.schedule(event_handler, str(self.mount_base), recursive=False)

    def scan_existing_mounts(self):
        """Scan and create shares for existing mounted devices"""
        self.logger.info("Scanning for existing mounted devices...")
        
        try:
            for item in self.mount_base.iterdir():
                if item.is_dir() and os.path.ismount(item):
                    self.logger.info(f"Found existing mount: {item}")
                    self.create_share(item)
                    
        except Exception as e:
            self.logger.error(f"Error scanning existing mounts: {e}")

    def start(self):
        """Start the SMB share manager"""
        self.logger.info("Starting SMB Share Manager...")
        
        # First scan for existing mounts
        self.scan_existing_mounts()
        
        # Start watching for new mounts
        self.observer.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.observer.stop()
        self.observer.join()

def main():
    logging.basicConfig(level=logging.DEBUG)
    try:
        manager = SMBShareManager()
        manager.start()
    except KeyboardInterrupt:
        print("\nStopping SMB Share Manager...")
    except Exception as e:
        logging.error(f"Critical error: {e}")
        raise

if __name__ == "__main__":
    main()