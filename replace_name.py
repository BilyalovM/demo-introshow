import os
import re

directory = '/Users/maximbilyalov/Documents/КОС/rental_app'

def replace_in_file(path):
    with open(path, 'r') as f:
        content = f.read()

    # Replacements
    new_content = content.replace('AINEX Studio', 'Intro Show')
    new_content = new_content.replace('Ainex Studio', 'Intro Show')
    new_content = new_content.replace('AINEX', 'Intro Show')
    new_content = new_content.replace('Ainex', 'Intro Show')
    new_content = new_content.replace('ainex', 'intro_show')

    if content != new_content:
        with open(path, 'w') as f:
            f.write(new_content)
        print(f"Updated {path}")

for root, dirs, files in os.walk(directory):
    if 'venv' in root or '.git' in root:
        continue
    for file in files:
        if file.endswith('.html') or file.endswith('.py'):
            path = os.path.join(root, file)
            # Skip this script
            if path.endswith('replace_name.py'):
                continue
            replace_in_file(path)

print("Done replacing text.")
