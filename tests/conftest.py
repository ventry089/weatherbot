import sys
from pathlib import Path

# add project root so `from weatherbot.common import ...` works
sys.path.insert(0, str(Path(__file__).parent.parent))
