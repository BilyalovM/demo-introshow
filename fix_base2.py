with open('templates/base.html', 'r') as f:
    lines = f.readlines()

out_lines = []
for line in lines:
    if line.strip() == '<a href="/inbox">':
        out_lines.append(' <a href="/inbox" class="nav-link">\n')
    elif line.strip() == '<a href="/assistant">':
        out_lines.append(' <a href="/assistant" class="nav-link">\n')
    elif line.strip() == '<a href="{{ url_for(\'read_settings\') }}">' or line.strip() == '<a href="{{ url_for(\'read_settings\') }}" class="">':
        out_lines.append(' <a href="{{ url_for(\'read_settings\') }}" class="nav-link">\n')
    else:
        out_lines.append(line)

with open('templates/base.html', 'w') as f:
    f.writelines(out_lines)

print("Fixed base.html")
