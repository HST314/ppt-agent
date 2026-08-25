from __future__ import annotations

import uvicorn
from configs.env_loader import load_dotenv

load_dotenv(".env")
def main() -> None:
    uvicorn.run("main_front:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
