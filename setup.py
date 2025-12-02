from setuptools import setup, find_packages

setup(
    name="odootools",
    version="1.0.1",
    packages=find_packages(),
    install_requires=["packaging", "bullet", "tqdm"],
    entry_points={
        "console_scripts": [
            "otools = odootools.main:main"
        ]
    },
)
