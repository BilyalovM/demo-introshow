import requests
from bs4 import BeautifulSoup

resp = requests.get('https://intro_showstudio.web.app/')
soup = BeautifulSoup(resp.text, 'html.parser')
aside = soup.find('aside')
if aside:
    print(aside.prettify())
else:
    print("No aside found")
