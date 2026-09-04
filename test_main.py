import sys
import traceback
from mailbrief.diagnostics.microsoft import main

try:
    main(["fetch"])
except Exception as e:
    traceback.print_exc()
