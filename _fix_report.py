with open('gui/naksha_cache/stream_manager.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Lines 2089-2101 are mangled - replace them with the correct continuation
# Line 2088: '            cpu_ms=cpu_ms,\n'
# Lines 2089-2100: the misplaced active_ptc_label method
# Line 2101: '        return True\n'

# Find the exact range
start = None
for i, line in enumerate(lines):
    if 'cpu_ms=cpu_ms,' in line and i < 2095:
        start = i
        break

if start is not None:
    # Find the end - 'return True' after the mangled method
    end = None
    for j in range(start + 1, min(start + 20, len(lines))):
        if lines[j].strip() == 'return True':
            end = j
            break
    
    if end is not None:
        # Replace from start+1 to end with the correct ptc_label line
        new_lines = [
            '            ptc_label=self.active_ptc_label())\n',
            '        return True\n',
        ]
        lines[start+1:end+1] = new_lines
        print(f'Replaced lines {start+2}-{end+1} with {len(new_lines)} correct lines')
    else:
        print(f'Could not find return True after line {start+1}')
        for j in range(start, min(start+15, len(lines))):
            print(f'{j+1:5d}| {repr(lines[j])}')

with open('gui/naksha_cache/stream_manager.py', 'w', encoding='utf-8', newline='\n') as f:
    f.writelines(lines)

# Also fix active_ptc_label to use getattr for app
old_def = 'path = str(getattr(self.app, "current_ptc_path", None) or "")'
new_def = 'path = str(getattr(app, "current_ptc_path", None) or "")'
if old_def in content:
    content = content.replace(old_def, new_def, 1)
    # Also add app = getattr line before it
    old_app = 'def active_ptc_label(self) -> str:'
    new_app = 'def active_ptc_label(self) -> str:\n        """B4: return PTC label for the status bar."""\n        app = getattr(self, "app", None)'
    # Just replace the old pattern
    pass

# Read back and fix the active_ptc_label method
with open('gui/naksha_cache/stream_manager.py', 'r') as f:
    content = f.read()

old_method = '''    def active_ptc_label(self) -> str:
        """B4: return the human-readable PTC label for the status bar.

        Returns the basename of the active .ptc file, or
        'Naksha Default' when the built-in palette is active. Never reads
        disk; the label is whatever the app currently holds.
        """
        path = str(getattr(self.app, "current_ptc_path", None) or "")'''

new_method = '''    def active_ptc_label(self) -> str:
        """B4: return the human-readable PTC label for the status bar.

        Returns the basename of the active .ptc file, or
        'Naksha Default' when the built-in palette is active. Never reads
        disk; the label is whatever the app currently holds.
        """
        app = getattr(self, "app", None)
        path = str(getattr(app, "current_ptc_path", None) or "")'''

if old_method in content:
    content = content.replace(old_method, new_method, 1)
    with open('gui/naksha_cache/stream_manager.py', 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)
    print('Fixed active_ptc_label to use getattr for app')
else:
    print('active_ptc_label method not found for app fix')

import ast
with open('gui/naksha_cache/stream_manager.py', 'r') as f:
    source = f.read()
ast.parse(source)
print('Syntax OK')
