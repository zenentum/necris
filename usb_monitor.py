#!/usr/bin/env python3

import pyudev
import subprocess
import os
import logging
from pathlib import Path
import time
import pwd
import grp
import psutil

class USBMonitor:
    def __init__(self):
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            filename='/var/log/usb_monitor.log'
        )
        self.logger = logging.getLogger(__name__)
        
        # Initialize pyudev
        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by(subsystem='block', device_type='partition')
        
        # Set default user to necris-user
        self.user = os.environ.get('SUDO_USER', 'necris-user')
        try:
            self.uid = pwd.getpwnam(self.user).pw_uid
            self.gid = grp.getgrnam(self.user).gr_gid
        except KeyError:
            self.logger.error(f"Could not find user {self.user}")
            raise SystemExit(f"User {self.user} not found in system")
        
        # Mount point base directory
        self.mount_base = Path(f'/media/{self.user}')
        self.setup_mount_directory()
        
        # Keep track of mounted devices
        self.mounted_devices = set()
        self.validate_existing_mounts()

    def validate_existing_mounts(self):
        """Validate existing mounts and clean up stale ones"""
        self.logger.info("Validating existing mounts...")
        partitions = psutil.disk_partitions(all=True)
        
        for partition in partitions:
            device_path = partition.device
            mount_point = partition.mountpoint
            
            # Check if this is one of our managed mount points
            if not str(mount_point).startswith(str(self.mount_base)):
                continue
                
            try:
                # Check if the device actually exists
                if not os.path.exists(device_path):
                    self.logger.warning(f"Found stale mount for missing device {device_path}")
                    try:
                        # Check if the mount point is actually mounted
                        if os.path.ismount(mount_point):
                            # Check if mount point is busy
                            lsof_check = subprocess.run(['lsof', mount_point], capture_output=True)
                            if lsof_check.returncode == 0:
                                self.logger.warning(f"Mount point {mount_point} is busy. Attempting lazy unmount...")
                                # Try lazy unmount
                                subprocess.run(['umount', '-l', mount_point], check=True, capture_output=True)
                            else:
                                # Regular force unmount
                                subprocess.run(['umount', '-f', mount_point], check=True, capture_output=True)
                            
                            self.logger.info(f"Successfully unmounted stale mount point {mount_point}")
                        else:
                            self.logger.info(f"Mount point {mount_point} is not actually mounted")
                        
                        # Clean up the mount point directory
                        mount_dir = Path(mount_point)
                        if mount_dir.exists():
                            # Check if directory is empty
                            if not os.listdir(mount_dir):
                                mount_dir.rmdir()
                                self.logger.info(f"Removed stale mount point directory {mount_point}")
                            else:
                                self.logger.warning(f"Mount point directory {mount_point} not empty, skipping removal")
                    except (subprocess.CalledProcessError, OSError) as e:
                        self.logger.error(f"Failed to clean up stale mount point {mount_point}: {e}")
                        # Could potentially add a notification here for manual intervention
                else:
                    # Device exists, verify it's actually a USB device
                    context = pyudev.Context()
                    device = pyudev.Devices.from_device_file(context, device_path)
                    
                    if self.is_usb_device(device):
                        self.logger.info(f"Validated existing USB mount: {device_path}")
                        self.mounted_devices.add(device_path)
                    else:
                        self.logger.warning(f"Found non-USB device mount in USB mount directory: {device_path}")
                        
            except Exception as e:
                self.logger.error(f"Error validating mount {mount_point}: {e}")

    def unmount_device(self, device_path):
        """Unmount the device with improved error handling"""
        device_name = os.path.basename(device_path)
        mount_point = self.mount_base / device_name

        try:
            # First check if it's actually mounted
            if not os.path.ismount(str(mount_point)):
                self.logger.info(f"Device {device_path} is not mounted at {mount_point}")
                # Clean up mount point if it exists but isn't mounted
                if mount_point.exists():
                    if not os.listdir(mount_point):  # Only if empty
                        mount_point.rmdir()
                return True

            # Check if mount point is busy
            lsof_check = subprocess.run(['lsof', str(mount_point)], capture_output=True)
            
            if lsof_check.returncode == 0:
                # Mount point is busy, try lazy unmount
                self.logger.warning(f"Mount point {mount_point} is busy, attempting lazy unmount")
                subprocess.run(['umount', '-l', str(mount_point)], check=True)
            else:
                # Try regular unmount first
                try:
                    subprocess.run(['umount', str(mount_point)], check=True)
                except subprocess.CalledProcessError:
                    # If regular unmount fails, try forced unmount
                    self.logger.warning(f"Regular unmount failed for {mount_point}, attempting force unmount")
                    subprocess.run(['umount', '-f', str(mount_point)], check=True)

            # Update mounted devices list
            self.mounted_devices.discard(device_path)
            
            # Remove mount point directory if empty
            if mount_point.exists():
                if not os.listdir(mount_point):  # Only if empty
                    mount_point.rmdir()
                else:
                    self.logger.warning(f"Mount point directory {mount_point} not empty, skipping removal")
                    
            self.logger.info(f"Successfully unmounted {device_path}")
            return True

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to unmount {device_path}: {e}")
            return False
        except OSError as e:
            self.logger.error(f"Failed to clean up mount point directory: {e}")
            return False

    def update_mounted_devices(self):
        """Update the set of currently mounted devices, verifying they still exist"""
        self.mounted_devices.clear()
        partitions = psutil.disk_partitions(all=True)
        for partition in partitions:
            device_path = partition.device
            if (os.path.exists(device_path) and  # Verify device still exists
                str(partition.mountpoint).startswith(str(self.mount_base))):  # Our mount
                self.mounted_devices.add(device_path)
                self.logger.debug(f"Found mounted device: {device_path}")

    def is_device_mounted(self, device_path):
        """Check if a device is already mounted"""
        return device_path in self.mounted_devices

    def is_usb_device(self, device):
        """Check if a device is a USB device"""
        for parent in device.ancestors:
            if parent.subsystem == 'usb':
                return True
        return False

    def setup_mount_directory(self):
        """Setup mount directory with proper permissions"""
        try:
            self.mount_base.mkdir(parents=True, exist_ok=True)
            # Give user full access to mount directory
            os.chown(self.mount_base, self.uid, self.gid)
            os.chmod(self.mount_base, 0o755)
            self.logger.info(f"Successfully set up mount directory for user {self.user}")
        except Exception as e:
            self.logger.error(f"Failed to setup mount directory: {e}")
            raise

    def set_permissions(self, path, is_directory=True):
        """Set appropriate permissions for mounted files and directories"""
        try:
            os.chown(path, self.uid, self.gid)
            if is_directory:
                # rwxr-xr-x for directories
                os.chmod(path, 0o755)
            else:
                # rw-r--r-- for files
                os.chmod(path, 0o644)
            return True
        except Exception as e:
            self.logger.error(f"Failed to set permissions for {path}: {e}")
            return False

    def recursively_set_permissions(self, path):
        """Recursively set permissions on a directory"""
        try:
            for root, dirs, files in os.walk(path):
                # Set directory permissions
                self.set_permissions(root, True)
                # Set file permissions
                for file in files:
                    self.set_permissions(os.path.join(root, file), False)
            return True
        except Exception as e:
            self.logger.error(f"Failed to recursively set permissions: {e}")
            return False

    def scan_existing_devices(self):
        """Scan and mount already connected USB devices"""
        # First validate existing mounts
        self.validate_existing_mounts()
        
        self.logger.info("Scanning for existing USB devices...")
        
        # Get all block devices
        for device in self.context.list_devices(subsystem='block', DEVTYPE='partition'):
            try:
                # Skip if not a USB device
                if not self.is_usb_device(device):
                    continue
                
                device_path = device.device_node
                
                # Skip if already mounted
                if self.is_device_mounted(device_path):
                    self.logger.info(f"Device {device_path} is already mounted")
                    continue
                
                self.logger.info(f"Found existing USB device: {device_path}")
                fs_type = self.get_filesystem_type(device_path)
                
                if fs_type:
                    self.logger.info(f"Mounting existing device {device_path} with filesystem {fs_type}")
                    self.mount_device(device_path, fs_type)
                    
            except Exception as e:
                self.logger.error(f"Error processing existing device {device.device_node}: {e}")

    def get_filesystem_type(self, device_path):
        """Detect filesystem type using blkid"""
        try:
            self.logger.debug(f"Attempting to detect filesystem type for {device_path}")
            for attempt in range(3):
                self.logger.debug(f"Attempt {attempt + 1} to detect filesystem type")
                result = subprocess.run(
                    ['blkid', '-o', 'value', '-s', 'TYPE', device_path],
                    capture_output=True,
                    text=True
                )
                self.logger.debug(f"blkid output: '{result.stdout.strip()}', stderr: '{result.stderr.strip()}'")
                if result.stdout.strip():
                    fs_type = result.stdout.strip()
                    self.logger.debug(f"Detected filesystem type: {fs_type}")
                    return fs_type
                time.sleep(1)
            self.logger.warning(f"Failed to detect filesystem type after 3 attempts for {device_path}")
            return None
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to detect filesystem type: {e}")
            return None


    def mount_device(self, device_path, filesystem_type):
        """Mount the device with appropriate filesystem type and permissions"""
        self.logger.debug(f"Attempting to mount {device_path} with filesystem type {filesystem_type}")
        
        # Skip if already mounted
        if self.is_device_mounted(device_path):
            self.logger.info(f"Device {device_path} is already mounted")
            return True

        device_name = os.path.basename(device_path)
        mount_point = self.mount_base / device_name
        self.logger.debug(f"Creating mount point at {mount_point}")
        mount_point.mkdir(exist_ok=True)
        os.chown(mount_point, self.uid, self.gid)

        # Prepare mount options based on filesystem type
        mount_options = []
        
        if filesystem_type == 'vfat':
            self.logger.debug("Using vfat mount options")
            mount_options.extend([
                f'uid={self.uid}',
                f'gid={self.gid}',
                'rw',
                'dmask=022',
                'fmask=133',
                'utf8',
                'flush'
            ])
        elif filesystem_type == 'ntfs':
            self.logger.debug("Using ntfs mount options")
            mount_options.extend([
                f'uid={self.uid}',
                f'gid={self.gid}',
                'rw',
                'dmask=022',
                'fmask=133',
                'windows_names',
                'big_writes',
                'ntfs-3g'
            ])
        elif filesystem_type == 'exfat':
            self.logger.debug("Using exfat mount options")
            mount_options.extend([
                f'uid={self.uid}',
                f'gid={self.gid}',
                'rw',
                'dmask=022',
                'fmask=133'
            ])
        elif filesystem_type in ['ext4', 'ext3', 'ext2']:
            self.logger.debug(f"Using {filesystem_type} mount options")
            mount_options.extend([
                'rw',
                'defaults',
                'user_xattr'
            ])

        # Build mount command
        mount_cmd = ['mount']
        if mount_options:
            mount_cmd.extend(['-o', ','.join(mount_options)])
        mount_cmd.extend([device_path, str(mount_point)])
        
        self.logger.debug(f"Mount command: {' '.join(mount_cmd)}")

        try:
            # Mount with retry mechanism
            for attempt in range(3):
                try:
                    subprocess.run(mount_cmd, check=True, capture_output=True, text=True)
                    self.logger.info(f"Successfully mounted {device_path} at {mount_point}")
                    
                    # Update mounted devices list
                    self.mounted_devices.add(device_path)
                    
                    # For ext filesystems, we need to set permissions after mounting
                    if filesystem_type in ['ext4', 'ext3', 'ext2']:
                        self.logger.debug("Setting permissions for ext filesystem...")
                        self.recursively_set_permissions(str(mount_point))
                    
                    # Verify mount was successful
                    if not os.path.ismount(str(mount_point)):
                        raise Exception("Mount point verification failed")
                    
                    return True
                except subprocess.CalledProcessError as e:
                    self.logger.error(f"Mount attempt {attempt + 1} failed: stdout='{e.stdout}', stderr='{e.stderr}'")
                    if attempt == 2:  # Last attempt
                        raise
                    time.sleep(1)
            return False
        except Exception as e:
            self.logger.error(f"Failed to mount {device_path}: {str(e)}")
            try:
                mount_point.rmdir()
            except OSError:
                pass
            return False

    def device_handler(self, device):
        """Handle device events"""
        if device.action == 'add':
            self.logger.info(f"New device detected: {device.device_node}")
            self.logger.debug(f"Device properties: {dict(device)}")
            time.sleep(1)  # Small delay to let system initialize device
            fs_type = self.get_filesystem_type(device.device_node)
            self.logger.debug(f"Filesystem type detection returned: {fs_type}")
            if fs_type:
                self.logger.info(f"Detected filesystem: {fs_type}")
                self.mount_device(device.device_node, fs_type)
            else:
                self.logger.warning(f"No filesystem type detected for {device.device_node}")
        elif device.action == 'remove':
            self.logger.info(f"Device removed: {device.device_node}")
            self.unmount_device(device.device_node)

    def start_monitoring(self):
        """Start monitoring for USB events"""
        self.logger.info(f"Starting USB monitor for user {self.user}...")
        
        # First scan for existing devices
        self.scan_existing_devices()
        
        # Then start monitoring for new events
        self.logger.info("Starting monitoring for new USB events...")
        self.monitor.start()
        for device in iter(self.monitor.poll, None):
            try:
                self.device_handler(device)
            except Exception as e:
                self.logger.error(f"Error handling device: {e}")
                continue

def main():
    logging.basicConfig(level=logging.DEBUG)
    monitor = USBMonitor()
    try:
        monitor.start_monitoring()
    except KeyboardInterrupt:
        print("\nStopping USB monitor...")
    except Exception as e:
        logging.error(f"Critical error: {e}")
        raise

if __name__ == "__main__":
    main()