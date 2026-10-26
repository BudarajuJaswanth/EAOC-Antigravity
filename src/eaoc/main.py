"""
EAOC Main Application Entry Point
Instantiates the FastAPI application using the create_app factory.
"""

from eaoc.api.factory import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    from eaoc.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "eaoc.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=(settings.ENVIRONMENT == "development"),
    )
