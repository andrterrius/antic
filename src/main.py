import asyncio
import urllib.parse
from flask import Flask, render_template, request
from json import load

app = Flask(__name__)

async def browser_run(cmd):
    try:
        proc = await asyncio.create_subprocess_shell(cmd)
    except:
        print("error")
@app.route("/")
def index():
    with open("antidetect.json") as file:
        profiles = load(file)
    return render_template("index.html", profiles=profiles['accounts'])

@app.route("/run", methods=["POST"])
def run():
    account = request.json
    asyncio.run(browser_run(f'python "пишем антик.py" --proxy {account["proxy"]} --useragent "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36" --profile {account["mail"]}'))
    return "ok"
app.run(debug=True)
