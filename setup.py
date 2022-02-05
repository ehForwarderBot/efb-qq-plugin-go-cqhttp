import sys

from setuptools import setup, find_packages

if sys.version_info < (3, 6):
    raise Exception("Python 3.6 or higher is required. Your version is %s." % sys.version)

__version__ = ""
exec(open('efb_qq_plugin_go_cqhttp/__version__.py').read())

long_description = open('README.rst').read()

setup(
    name='efb-qq-plugin-go-cqhttp',
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    version=__version__,
    description='EQS plugin for Go-CQHttp API Compatible Client.',
    long_description=long_description,
    include_package_data=True,
    author='XYenon',
    author_email='i@xyenon.bid',
    url='https://github.com/XYenon/efb-qq-plugin-go-cqhttp',
    license='AGPLv3',
    python_requires='>=3.6',
    keywords=['ehforwarderbot', 'EH Forwarder Bot', 'EH Forwarder Bot Slave Channel',
              'qq', 'chatbot', 'EQS', 'CoolQ', 'go-cqhttp'],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Intended Audience :: Developers",
        "Intended Audience :: End Users/Desktop",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Topic :: Communications :: Chat",
        "Topic :: Utilities"
    ],
    install_requires=[
        "efb-qq-slave", "ehforwarderbot",
        "PyYaml",
        'requests', 'python-magic', 'Pillow', 'cqhttp>=1.3.0', 'cherrypy>=18.5.0'
    ],
    entry_points={
        'ehforwarderbot.qq.plugin': 'GoCQHttp = efb_qq_plugin_go_cqhttp:GoCQHttp'
    }
)
