"""Global pytest configuration and environment hooks."""

import os

# Ensure Qt runs in offscreen mode in headless test environments
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
