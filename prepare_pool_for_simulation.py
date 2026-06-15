import json
from pathlib import Path

from web3 import Web3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POOL_ADDRESS = "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7"
RPC_URL = "http://192.168.1.12:8545"
POOL_INDEX_PATH = Path("pool_index.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_address(address):
    if not address:
        return None
    address = str(address).strip()
    if not address or address.lower() == "0x" + "0" * 40:
        return None
    return Web3.to_checksum_address(address)


def load_json(path):
    return json.loads(Path(path).read_text())


def find_pool_record(pool_address, index):
    target = normalize_address(pool_address).lower()
    for record in index.get("pools", []):
        if normalize_address(record.get("pool_address")).lower() == target:
            return record
    raise ValueError(f"pool {pool_address} not found in {POOL_INDEX_PATH}")


def resolve_abi_path(record):
    impl = normalize_address(record.get("implementation_address"))
    if not impl:
        raise ValueError(f"no implementation_address for {record.get('pool_address')}")
    path = Path("pool_implementations") / impl / "abi.json"
    if not path.exists():
        raise FileNotFoundError(f"implementation ABI not found: {path}")
    return path


def is_integer_type(abi_type):
    return abi_type in ("uint256", "int128", "int256", "uint128")


def coin_count(record):
    args = record.get("deployment_details", {}).get("args", {})
    coins = args.get("coins") or args.get("_coins") or []
    return len(coins)


# Minimal ERC20 ABI for balanceOf/totalSupply.
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "totalSupply", "type": "uint256"}],
        "type": "function",
    },
]


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------


def build_w3():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise RuntimeError(f"RPC not connected: {RPC_URL}")
    return w3


def call_zero_arg_views(contract, abi, block_number):
    out = {}
    for item in abi:
        if item.get("type") != "function":
            continue
        if not item.get("name"):
            continue
        if item.get("inputs"):
            continue
        mutability = item.get("stateMutability") or ""
        if mutability not in ("view", "pure") and not item.get("constant"):
            continue

        name = item["name"]
        try:
            result = getattr(contract.functions, name)().call(block_identifier=block_number)
            out[name] = result
        except Exception as exc:
            out[name] = {"error": repr(exc)}
    return out


def call_indexed_views(contract, abi, n_coins, block_number):
    out = {}
    for item in abi:
        if item.get("type") != "function":
            continue
        if not item.get("name"):
            continue
        inputs = item.get("inputs", [])
        if len(inputs) != 1:
            continue
        if not is_integer_type(inputs[0].get("type", "")):
            continue
        mutability = item.get("stateMutability") or ""
        if mutability not in ("view", "pure") and not item.get("constant"):
            continue

        name = item["name"]
        values = []
        for i in range(n_coins):
            try:
                result = getattr(contract.functions, name)(i).call(block_identifier=block_number)
                values.append(result)
            except Exception as exc:
                values.append({"error": repr(exc)})
        out[name] = values
    return out


def fetch_coins_data(record, contract, abi, block_number):
    args = record.get("deployment_details", {}).get("args", {})
    raw_coins = args.get("coins") or args.get("_coins") or []
    n_coins = len(raw_coins)

    # Try to get any per-coin arrays we already collected via the indexed pass.
    parameters = {}
    parameters.update(call_zero_arg_views(contract, abi, block_number))
    parameters.update(call_indexed_views(contract, abi, n_coins, block_number))

    coins = []
    for i, coin in enumerate(raw_coins):
        if isinstance(coin, dict):
            address = coin.get("address")
            meta = dict(coin)
        else:
            address = coin
            meta = {}

        checksum = normalize_address(address)
        balance = None
        if checksum:
            try:
                token = contract.w3.eth.contract(address=checksum, abi=ERC20_ABI)
                balance = token.functions.balanceOf(record["pool_address"]).call(block_identifier=block_number)
            except Exception as exc:
                balance = {"error": repr(exc)}

        coin_info = {
            "index": i,
            "address": checksum,
            "balance": balance,
        }
        # Attach any per-coin values we have by index.
        for key, value in parameters.items():
            if isinstance(value, list) and len(value) == n_coins:
                coin_info[key] = value[i]
        coin_info.update(meta)
        coins.append(coin_info)

    return coins, parameters


def fetch_lp_total_supply(record, contract, block_number):
    pool_address = normalize_address(record["pool_address"])
    lp_address = normalize_address(record.get("lp_token_address")) or pool_address

    try:
        # Some pools expose totalSupply directly.
        if hasattr(contract.functions, "totalSupply"):
            return contract.functions.totalSupply().call(block_identifier=block_number)
    except Exception:
        pass

    try:
        token = contract.w3.eth.contract(address=lp_address, abi=ERC20_ABI)
        return token.functions.totalSupply().call(block_identifier=block_number)
    except Exception as exc:
        return {"error": repr(exc)}


def build_snapshot(pool_address, w3=None, seen=None):
    if w3 is None:
        w3 = build_w3()
    if seen is None:
        seen = set()

    pool_address = normalize_address(pool_address)
    if pool_address.lower() in seen:
        return None
    seen.add(pool_address.lower())

    index = load_json(POOL_INDEX_PATH)
    record = find_pool_record(pool_address, index)

    abi_path = resolve_abi_path(record)
    abi = load_json(abi_path)

    block_number = w3.eth.block_number
    block_timestamp = w3.eth.get_block(block_number)["timestamp"]

    contract = w3.eth.contract(address=pool_address, abi=abi)
    contract.w3 = w3  # attach so helper functions can reach w3 easily

    n_coins = coin_count(record)
    parameters = {}
    parameters.update(call_zero_arg_views(contract, abi, block_number))
    parameters.update(call_indexed_views(contract, abi, n_coins, block_number))

    coins, _ = fetch_coins_data(record, contract, abi, block_number)
    lp_total_supply = fetch_lp_total_supply(record, contract, block_number)

    snapshot = {
        "pool_address": pool_address,
        "implementation_address": normalize_address(record.get("implementation_address")),
        "abi": abi,
        "abi_source": str(abi_path),
        "w3": w3,
        "contract": contract,
        "block_number": block_number,
        "block_timestamp": block_timestamp,
        "pool_record": record,
        "parameters": parameters,
        "coins": coins,
        "lp_total_supply": lp_total_supply,
        "base_pool": None,
    }

    # Recurse into base pool for metapools.
    args = record.get("deployment_details", {}).get("args", {})
    if args.get("isMetaPool"):
        base_pool_address = args.get("base_pool_address")
        if base_pool_address:
            snapshot["base_pool"] = build_snapshot(base_pool_address, w3=w3, seen=seen)

    return snapshot


def print_summary(snapshot):
    param_count = len(snapshot["parameters"])
    error_count = sum(1 for v in snapshot["parameters"].values() if isinstance(v, dict) and "error" in v)
    coin_count = len(snapshot["coins"])
    base_loaded = snapshot["base_pool"] is not None

    print(f"Pool:            {snapshot['pool_address']}")
    print(f"Implementation:  {snapshot['implementation_address']}")
    print(f"ABI source:      {snapshot['abi_source']}")
    print(f"Block:           {snapshot['block_number']}  (ts={snapshot['block_timestamp']})")
    print(f"Parameters:      {param_count} fetched, {error_count} reverted")
    print(f"Coins:           {coin_count}")
    print(f"Base pool loaded: {'yes' if base_loaded else 'no'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    snapshot = build_snapshot(POOL_ADDRESS)
    print_summary(snapshot)
    return snapshot


if __name__ == "__main__":
    snapshot = main()
