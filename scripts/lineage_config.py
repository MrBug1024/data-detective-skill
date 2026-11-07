"""Validate public configuration before touching source data."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft7Validator
    schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/config.schema.json').read_text(encoding='utf-8'))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def validate_config(config):
    if config is None or isinstance(config, dict) and not config:
        return {}
    error = next(_validator().iter_errors(config), None)
    if error:
        location = '.'.join(str(part) for part in error.absolute_path) or '<root>'
        raise ValueError(f'Invalid lineage config at {location}: {error.message}')
    return config
