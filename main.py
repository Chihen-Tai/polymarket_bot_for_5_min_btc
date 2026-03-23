from core.runner import main
import logging
import ssl

try:
    _create_unverified_https_context = getattr(ssl, '_create_unverified_context')
    ssl._create_default_https_context = _create_unverified_https_context
except AttributeError:
    pass

if __name__ == '__main__':
    main()
