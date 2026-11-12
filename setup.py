#!/usr/bin/env python
from setuptools import setup

setup(
    python_requires=">=3.7.1",
    install_requires=[
        # 3.7: legacy pin. 3.9+ (incl. 3.10/3.14): modern stack.
        'pandas==1.3.5; python_version < "3.9"',
        'pandas>=2.3.3; python_version >= "3.9"',
        "singer-python>=5.0.12",
        "requests>=2.28.0",
    ],
)
