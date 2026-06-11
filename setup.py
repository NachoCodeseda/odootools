from setuptools import setup, find_packages

setup(
    name="odootools",
    version="1.1.2",
    packages=find_packages(),
    install_requires=["packaging", "bullet", "tqdm"],
    entry_points={
        "console_scripts": [
            "otools = odootools.main:main"
        ]
    },
)
