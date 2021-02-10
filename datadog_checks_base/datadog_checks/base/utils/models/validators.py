# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from .types import make_immutable_check_config


def finalize_config(values):
    # This is what is returned by the final root validator of each config model. Note:
    #
    # 1. the final object must be a dict
    # 2. we maintain the original order of keys
    return {field: make_immutable_check_config(value) for field, value in values.items()}
