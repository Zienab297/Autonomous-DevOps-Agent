from pyngrok import ngrok

url = ngrok.connect(8000)
print(url)