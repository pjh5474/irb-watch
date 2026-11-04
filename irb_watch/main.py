from fastapi import FastAPI

from irb_watch.api import health

app = FastAPI(title="IRB Watch")
app.include_router(health.router)
