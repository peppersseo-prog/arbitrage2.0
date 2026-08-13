from adapters import BybitAdapter, OKXAdapter, BitgetAdapter

class ExchangeManager:
    ADAPTERS = {"bybit": BybitAdapter, "okx": OKXAdapter, "bitget": BitgetAdapter}

    def __init__(self, credentials=None):
        self.credentials = credentials or {}
        self.instances = {}

    def get(self, name):
        name = name.lower()
        if name not in self.ADAPTERS:
            raise ValueError(f"Unsupported exchange: {name}")
        if name not in self.instances:
            c = self.credentials.get(name, {})
            self.instances[name] = self.ADAPTERS[name](
                c.get("api_key", ""), c.get("api_secret", "")
            )
        return self.instances[name]
