# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from immutables import Map


def make_immutable_check_config(obj):
    # Only consider container types that can be de-serialized from YAML/JSON
    if isinstance(obj, list):
        return tuple(make_immutable_check_config(item) for item in obj)
    elif isinstance(obj, dict):
        # There are no ordering guarantees, see https://github.com/MagicStack/immutables/issues/57
        return Map((k, make_immutable_check_config(v)) for k, v in obj.items())

    return obj
