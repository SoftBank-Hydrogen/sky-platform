"""A root ASGI app that needs a generated deployment image."""
from fastapi import FastAPI


app = FastAPI()


@app.get("/")
def home():
    return {"message": "FastAPI application is running"}
