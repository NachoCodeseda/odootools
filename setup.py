from setuptools import setup, find_packages

setup(
    name="odootools",
    version="1.2.4",
    packages=find_packages(),
    install_requires=["packaging", "bullet"],
    entry_points={
        "console_scripts": [
            "otools = odootools.main:main"
        ]
    },
)
