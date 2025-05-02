import os
import json
import psutil
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class DiskThresholds:
    warning: int = 75  # Default warning at 75% usage
    critical: int = 90 # Default critical at 90% usage

class DiskMonitor:
    def __init__(self, base_path: str, config_path: str = '/etc/necris/disk_config.json'):
        self.base_path = base_path
        self.config_path = config_path
        self.thresholds = self._load_thresholds()
    
    def _load_thresholds(self) -> DiskThresholds:
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    config = json.load(f)
                return DiskThresholds(
                    warning=config.get('warning_threshold', 75),
                    critical=config.get('critical_threshold', 90)
                )
        except Exception:
            pass
        return DiskThresholds()
    
    def save_thresholds(self, warning: int, critical: int) -> bool:
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w') as f:
                json.dump({
                    'warning_threshold': warning,
                    'critical_threshold': critical
                }, f)
            self.thresholds = DiskThresholds(warning=warning, critical=critical)
            return True
        except Exception:
            return False
    
    def get_mounted_drives(self) -> List[Dict]:
        """Get all mounted drives in the base directory"""
        drives = []
        try:
            for item in os.scandir(self.base_path):
                if item.is_dir():
                    drive_path = item.path
                    try:
                        usage = psutil.disk_usage(drive_path)
                        used_percent = usage.percent
                        status = 'normal'
                        
                        if used_percent >= self.thresholds.critical:
                            status = 'critical'
                        elif used_percent >= self.thresholds.warning:
                            status = 'warning'
                            
                        drives.append({
                            'name': item.name,
                            'path': drive_path,
                            'total': usage.total,
                            'used': usage.used,
                            'free': usage.free,
                            'percent': used_percent,
                            'status': status
                        })
                    except (PermissionError, OSError):
                        # Skip drives that can't be accessed
                        continue
        except Exception as e:
            print(f"Error scanning drives: {e}")
        
        return drives
    
    def get_all_disk_usage(self) -> Dict:
        """Get usage for all mounted drives and threshold settings"""
        return {
            'drives': self.get_mounted_drives(),
            'thresholds': {
                'warning': self.thresholds.warning,
                'critical': self.thresholds.critical
            }
        }