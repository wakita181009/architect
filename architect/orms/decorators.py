"""
Defines decorators that Architect provides.
"""

import inspect
import functools

from .bases import BaseFeature
from .registry import registry, Registrar
from ..exceptions import (
    ORMError,
    FeatureInstallError,
    FeatureUninstallError,
    MethodAutoDecorateError
)


class install(object):
    """
    Install decorator installs the requested feature for a model. All features are
    installed inside the "architect" namespace to avoid all possible naming conflicts.
    """
    def __init__(self, feature, **options):
        """
        :param string feature: (required). A feature to install.
        :param dictionary options: (optional). Feature options.
        """
        self.features = {}
        self.feature = feature
        self.options = {'feature': options, 'global': dict((k, v) for k, v in options.items() if k in ('db',))}

    def __call__(self, model):
        """
        :param class model: (required). A model class where feature will be installed.
        """
        orm = self.options['feature'].pop('orm', None) or model.__class__.__module__.split('.')[0]

        if orm not in Registrar.orms:
            try:
                __import__('{0}.features'.format(orm), globals(), level=1, fromlist='*')
            except ImportError:
                import os
                import pkgutil
                raise ORMError(
                    current=orm,
                    model=model.__name__,
                    allowed=[name for _, name, is_pkg in pkgutil.iter_modules([os.path.dirname(__file__)]) if is_pkg])

        self.init_feature(self.feature, model, registry[orm])

        # If a model already has some architect features installed, we need to
        # gather them and merge with the new feature that needs to be installed
        if hasattr(model, 'architect'):
            for name, obj in model.architect.__dict__.items():
                if isinstance(obj, BaseFeature):
                    if name not in self.features:
                        self.features[name] = {'class': obj.__class__, 'options': obj.options}

        # Some ORMs disallow setting new attributes on model classes, we
        # have to fix this by providing the default __setattr__ behaviour
        type(model).__setattr__ = lambda o, n, v: type.__setattr__(o, n, v)

        # So what's going on here ? The idea is to create an "architect" namespace using the
        # Architect class which is a descriptor itself, which when accessed returns the
        # autogenerated class with all the requested features. The most important part here
        # is that because we're using a descriptor we can get access to the model class from
        # every feature class and all that absolutely correctly works even with the model
        # inheritance. While the same can be achieved using metaclasses, the problem is that
        # every ORM also uses metaclasses which produces the metaclass conflict because a
        # class can't have two metaclasses. This situation can also be solved but it requires
        # much more magical stuff to be written that is why this approach was chosen
        class Architect(object):
            def __init__(self, features):
                self.map = {}
                self.features = features

            def __get__(self, model_obj, model_cls):
                # If a model class accesses an architect namespace for the first
                # time, we need to put it inside a map for the future reference
                if model_cls not in self.map:
                    self.map[model_cls] = {'features': {}}

                    for feature, options in self.features.items():
                        self.map[model_cls]['features'][feature] = options['class'](
                            model_obj, model_cls, **options['options'])

                    self.map[model_cls]['architect'] = type('Architect', (object,), dict(
                        self.map[model_cls]['features'], **{'__module__': 'architect'}))

                # We have to notify each feature object if a model object wants
                # to get access to it, otherwise it won't have an idea about it
                if model_obj is not None:
                    for feature in self.map[model_cls]['features']:
                        self.map[model_cls]['features'][feature].model_obj = model_obj

                return self.map[model_cls]['architect']

        model.architect = Architect(self.features)
        return model

    def init_feature(self, feature, model, features_registry):
        """
        Initializes the requested feature.

        :param string feature: (required). A feature to initialize.
        :param class model: (required). A model where feature will be initialized.
        :param dict features_registry: (required). A registry with available features for the current ORM.
        """
        try:
            feature_cls = features_registry[feature]
        except KeyError:
            raise FeatureInstallError(current=feature, model=model.__name__, allowed=features_registry.keys())

        for name in feature_cls.decorate:
            try:
                original = getattr(model, name)

                if getattr(original, 'is_decorated', False):  # Handle the inheritance cases
                    original = original.original
            except AttributeError:
                raise MethodAutoDecorateError(current=name, model=model.__name__)

            decorator = getattr(feature_cls, '_decorate_{0}'.format(name))
            decorated = functools.wraps(original)(decorator(original))
            decorated.original = original
            decorated.is_decorated = True
            setattr(model, name, decorated)

        self.features[feature] = {
            'class': feature_cls,
            'options': self.options['feature'] if feature == self.feature else self.options['global']
        }

        if hasattr(feature_cls, 'register_hooks'):
            feature_cls.register_hooks(model)

        for dependency in feature_cls.dependencies:
            self.init_feature(dependency, model, features_registry)


class uninstall(object):
    """
    Uninstall decorator uninstalls the requested feature and all it's dependencies from a model.
    """
    def __init__(self, feature):
        """
        :param string feature: (required). A feature to uninstall.
        """
        self.feature = feature

    def __call__(self, model):
        """
        :param class model: (required). A model class to work with.
        """
        self.deinit_feature(self.feature, model)
        return model

    def deinit_feature(self, feature, model):
        """
        Deinitializes requested feature and it's dependencies.

        :param string feature: (required). A feature to deinitialize.
        :param class model: (required). A model class to work with.
        """
        try:
            feature_obj = getattr(model.architect, feature)
        except AttributeError:
            raise FeatureUninstallError(
                current=feature,
                model=model.__name__,
                allowed=[name for name, obj in model.architect.__dict__.items() if isinstance(obj, BaseFeature)])

        # The concept of "unbound methods" has been removed from Python 3. When accessing a method
        # from a class, we now get a plain function object. This is what the isfunction check for
        methods = inspect.getmembers(model, predicate=lambda m: inspect.isfunction(m) or inspect.ismethod(m))

        for name, method in methods:
            if getattr(method, 'is_decorated', False):
                setattr(model, name, method.original)

        delattr(model.architect, feature)  # TODO: prohibit uninstall if there are dependant features

        for dependency in feature_obj.dependencies:
            self.deinit_feature(dependency, model)
