from setuptools import setup, find_packages

setup(
    name="odootools",
    version="1.2.6",
    packages=find_packages(),
    install_requires=["packaging"],
    entry_points={
        "console_scripts": [
            "otools = odootools.main:main",
        ]
    },
)
