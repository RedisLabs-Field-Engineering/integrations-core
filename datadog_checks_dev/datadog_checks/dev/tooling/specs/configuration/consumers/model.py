# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import yaml
from datamodel_code_generator.format import CodeFormatter, PythonVersion
from datamodel_code_generator.parser import LiteralType
from datamodel_code_generator.parser.openapi import OpenAPIParser

from ..utils import default_option_example, sanitize_openapi_object_properties

PYTHON_VERSION = PythonVersion.PY_38
EXTRA_TYPE_FIELDS = ('compact_example', 'default', 'display_default', 'example')

# Singleton allowing `None` to be a valid default value
NO_DEFAULT = object()


def normalize_option_name(option_name):
    return option_name.replace('-', '_')


def example_looks_informative(example):
    return '<' in example and '>' in example and example == example.upper()


def get_default_value(option_name, type_data):
    if 'default' in type_data:
        return type_data['default']
    elif 'type' not in type_data or type_data['type'] in ('array', 'object'):
        return NO_DEFAULT

    example = type_data['example']
    if example == default_option_example(option_name):
        return NO_DEFAULT
    elif type_data['type'] == 'string' and example_looks_informative(example):
        return NO_DEFAULT

    return example


def add_imports(model_file_lines, need_defaults):
    import_lines = []

    for i, line in enumerate(model_file_lines):
        if line.startswith('from '):
            import_lines.append(i)

    # pydantic imports
    final_import_line = import_lines[-1]
    model_file_lines[final_import_line] += ', root_validator, validator'

    local_import_start_location = final_import_line + 1
    for line in reversed(
        (
            '',
            'from datadog_checks.base.utils.functions import identity',
            'from datadog_checks.base.utils.models.validators import finalize_config',
            '',
            'from . import defaults, validators' if need_defaults else 'from . import validators',
        )
    ):
        model_file_lines.insert(local_import_start_location, line)


class ModelConsumer:
    def __init__(self, spec, code_formatter=None):
        self.spec = spec
        self.code_formatter = code_formatter or self.create_code_formatter()

    def render(self):
        files = {}

        for file in self.spec['files']:
            # (<file name>, (<contents>, <errors>))
            model_files = {}

            root_imports = []
            defaults_file_lines = []
            defaults_file_needs_dynamic_values = False
            defaults_file_needs_value_normalization = False

            for section in sorted(file['options'], key=lambda s: s['name']):
                errors = []

                section_name = section['name']
                if section_name == 'init_config':
                    model_id = 'shared'
                    model_file_name = f'{model_id}.py'
                    schema_name = 'SharedConfig'
                elif section_name == 'instances':
                    model_id = 'instance'
                    model_file_name = f'{model_id}.py'
                    schema_name = 'InstanceConfig'
                # Skip anything checks don't use directly
                else:
                    continue

                root_imports.append(f'from .{model_id} import {schema_name}')

                # We want to create something like:
                #
                # components:
                #   schemas:
                #     InstanceConfig:
                #       required:
                #       - endpoint
                #       properties:
                #         endpoint:
                #           ...
                #         timeout:
                #           ...
                #         ...
                openapi_document = {'components': {'schemas': {}}}
                schema = openapi_document['components']['schemas'][schema_name] = {}

                options = schema['properties'] = {}
                required_options = schema['required'] = []
                options_with_defaults = False

                for option in sorted(section['options'], key=lambda o: o['name']):
                    option_name = option['name']
                    normalized_option_name = normalize_option_name(option_name)

                    if 'value' in option:
                        type_data = option['value']
                    # Some integrations (like `mysql`) have options that are grouped under a top-level option
                    elif 'options' in option:
                        nested_properties = []
                        type_data = {'type': 'object', 'properties': nested_properties}
                        for nested_option in option['options']:
                            nested_type_data = nested_option['value']

                            # Remove fields that aren't part of the OpenAPI specification
                            for extra_field in EXTRA_TYPE_FIELDS:
                                nested_type_data.pop(extra_field, None)

                            nested_properties.append({'name': nested_option['name'], **nested_type_data})
                    else:
                        errors.append(f'Option `{option_name}` must have a `value` or `options` attribute')
                        continue

                    options[option_name] = type_data

                    if option['required']:
                        required_options.append(option_name)
                    else:
                        options_with_defaults = True
                        defaults_file_lines.append('')
                        defaults_file_lines.append('')
                        defaults_file_lines.append(f'def {model_id}_{normalized_option_name}(field, value):')

                        default_value = get_default_value(option_name, type_data)
                        if default_value is not NO_DEFAULT:
                            defaults_file_needs_value_normalization = True
                            defaults_file_lines.append(f'    return {default_value!r}')
                        else:
                            defaults_file_needs_dynamic_values = True
                            defaults_file_lines.append('    return get_default_field_value(field, value)')

                    # Remove fields that aren't part of the OpenAPI specification
                    for extra_field in EXTRA_TYPE_FIELDS:
                        type_data.pop(extra_field, None)

                    sanitize_openapi_object_properties(type_data)

                try:
                    parser = OpenAPIParser(
                        yaml.safe_dump(openapi_document),
                        target_python_version=PythonVersion.PY_38,
                        enum_field_as_literal=LiteralType.All,
                        encoding='utf-8',
                        use_generic_container_types=True,
                        enable_faux_immutability=True,
                        # TODO: uncomment when the Agent upgrades Python to 3.9
                        # use_standard_collections=True,
                        strip_default_none=True,
                        # https://github.com/koxudaxi/datamodel-code-generator/pull/173
                        field_constraints=True,
                    )
                    model_file_contents = parser.parse()
                except Exception as e:
                    errors.append(f'Error parsing the OpenAPI schema `{schema_name}`: {e}')
                    model_files[model_file_name] = ('', errors)
                    continue

                model_file_lines = model_file_contents.splitlines()
                add_imports(model_file_lines, options_with_defaults)

                model_file_lines.append('')
                model_file_lines.append('    @root_validator(pre=True)')
                model_file_lines.append('    def _initial_validation(cls, values):')
                model_file_lines.append(
                    f"        return getattr(validators, 'initialize_{model_id}', identity)(values)"
                )

                model_file_lines.append('')
                model_file_lines.append("    @validator('*', pre=True, always=True)")
                model_file_lines.append('    def _ensure_defaults(cls, v, field):')
                model_file_lines.append('        if v is not None or field.required:')
                model_file_lines.append('            return v')
                model_file_lines.append('')
                model_file_lines.append(f"        return getattr(defaults, f'{model_id}_{{field.name}}')(field, v)")

                model_file_lines.append('')
                model_file_lines.append("    @validator('*')")
                model_file_lines.append('    def _run_validations(cls, v, field):')
                # TODO: remove conditional when there is a workaround:
                # https://github.com/samuelcolvin/pydantic/issues/2376
                model_file_lines.append('        if not v:')
                model_file_lines.append('            return v')
                model_file_lines.append('')
                model_file_lines.append(
                    f"        return getattr(validators, f'{model_id}_{{field.name}}', identity)(v, field=field)"
                )

                model_file_lines.append('')
                model_file_lines.append('    @root_validator(pre=False)')
                model_file_lines.append('    def _final_validation(cls, values):')
                model_file_lines.append(
                    f"        return finalize_config(getattr(validators, 'finalize_{model_id}', identity)(values))"
                )

                model_file_lines.append('')
                model_file_contents = '\n'.join(model_file_lines)
                if any(len(line) > 120 for line in model_file_lines):
                    model_file_contents = self.code_formatter.apply_black(model_file_contents)

                model_files[model_file_name] = (model_file_contents, errors)

            # Logs-only integrations
            if not model_files:
                continue

            if defaults_file_lines:
                if defaults_file_needs_dynamic_values:
                    defaults_file_lines.insert(
                        0, 'from datadog_checks.base.utils.models.fields import get_default_field_value'
                    )

                defaults_file_lines.append('')
                defaults_file_contents = '\n'.join(defaults_file_lines)
                if defaults_file_needs_value_normalization:
                    defaults_file_contents = self.code_formatter.apply_black(defaults_file_contents)

                model_files['defaults.py'] = (defaults_file_contents, [])

            root_imports.sort()
            root_imports.append('')
            model_files['__init__.py'] = ('\n'.join(root_imports), [])
            model_files['validators.py'] = ('', [])

            files[file['name']] = {file_name: model_files[file_name] for file_name in sorted(model_files)}

        return files

    @staticmethod
    def create_code_formatter():
        return CodeFormatter(PYTHON_VERSION)
