from pathlib import Path


def load_environment(path):
    if path is None:
        return
    from dotenv import load_dotenv
    path = Path(path)
    if not path.is_file():
        raise ValueError("environment file does not exist")
    load_dotenv(path, override=False)
