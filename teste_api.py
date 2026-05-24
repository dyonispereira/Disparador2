from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def raiz():
    return {"msg": "API OK"}

@app.get("/status")
def status():
    return {"status": "rodando"}