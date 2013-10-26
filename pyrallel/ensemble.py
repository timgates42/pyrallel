"""Tools for build ensembles on distributed compute resources

Author: Olivier Grisel <olivier@ogrisel.com>
Licensed: MIT
"""

import uuid
import os
from random import Random
from copy import copy
from collections import namedtuple

from IPython.parallel import interactive

from sklearn.base import clone
from sklearn.externals import joblib
from pyrallel.common import TaskManager


def combine(all_ensembles):
    """Combine the sub-estimators of a group of ensembles

        >>> from sklearn.datasets import load_iris
        >>> from sklearn.ensemble import ExtraTreesClassifier
        >>> iris = load_iris()
        >>> X, y = iris.data, iris.target

        >>> all_ensembles = [ExtraTreesClassifier(n_estimators=4).fit(X, y)
        ...                  for i in range(3)]
        >>> big = combine(all_ensembles)
        >>> len(big.estimators_)
        12
        >>> big.n_estimators_
        12
        >>> big.score(X, y)
        1.0

    """
    final_ensemble = copy(all_ensembles[0])

    for ensemble in all_ensembles[1:]:
        final_ensemble.estimators_ += ensemble.estimators_

    final_ensemble.n_estimators_ = len(final_ensemble.estimators_)
    return final_ensemble


def sub_ensemble(ensemble, n_estimators, seed=None):
    """Build a new ensemble with a random subset of the sub-estimators

        >>> from sklearn.datasets import load_iris
        >>> from sklearn.ensemble import ExtraTreesClassifier
        >>> iris = load_iris()
        >>> X, y = iris.data, iris.target

        >>> big = ExtraTreesClassifier(n_estimators=10).fit(X, y)
        >>> small = sub_ensemble(big, 3)
        >>> len(small.estimators_)
        3
        >>> small.n_estimators_
        3
        >>> big.score(X, y)
        1.0

    """
    rng = Random(seed)
    final_ensemble = copy(ensemble)
    if n_estimators > len(ensemble.estimators_):
        raise ValueError(
            "Cannot sample %d estimators from ensemble of %d"
            % (n_estimators, len(ensemble.estimators_)))

    final_ensemble.estimators_ = rng.sample(
        ensemble.estimators_, n_estimators)

    final_ensemble.n_estimators_ = len(final_ensemble.estimators_)
    return final_ensemble


@interactive
def train_model(model, data_filename, model_filename, random_state=None):
    from sklearn.externals import joblib

    # Memory map the data
    X, y = joblib.load(data_filename, mmap_mode='r')

    # Train the model
    model.set_params(random_state=random_state)
    model.fit(X, y)

    # Clean the random_state attributes to reduce the amount
    # of useless numpy arrays that will be created on the
    # filesystem
    for estimator in model.estimators_:
        if hasattr(estimator, 'tree_'):
            estimator.tree_.random_state = 0

    # Save the model back to the FS as it can be large
    joblib.dump(model, model_filename)

    return model_filename


class EnsembleGrower(TaskManager):
    """Distribute computation of sklearn ensembles

    This works for averaging ensembles like random forests
    or bagging ensembles.

    Does not work with sequential ensembles such as AdaBoost or
    GBRT.

    """

    def __init__(self, load_balanced_view, base_model):
        self.tasks = []
        self.base_model = base_model
        self.lb_view = load_balanced_view
        self._temp_files = []

    def reset(self):
        # Abort any other previously scheduled tasks
        self.abort()

        # Forget about the old tasks
        self.tasks[:] = []

        # Collect temporary files:
        for filename in self._temp_files:
            os.unlink(filename)
        del self._temp_files[:]

    def launch(self, X, y, n_estimators=1, pre_warm=True,
               folder=".", name=None):
        self.reset()
        if name is None:
            name = uuid.uuid4().get_hex()

        if not os.path.exists(folder):
            os.makedirs(folder)

        data_filename = os.path.join(folder, name + '_data.pkl')
        self._temp_files.extend(joblib.dump((X, y), data_filename))

        for i in range(n_estimators):
            base_model = clone(self.base_model)
            model_filename = os.path.join(
                folder, name + '_model_%03d_pkl' % i)
            self.tasks.append(self.lb_view.apply(
                train_model, base_model, data_filename, model_filename,
                random_state=i))
        # Make it possible to chain method calls
        return self

    def report(self, n_top=5):
        output = "Progress: {0:02d}% ({1:03d}/{2:03d})\n".format(
            int(100 * self.progress()), self.completed(), self.total())
        return output

    def __repr__(self):
        return self.report()

    def aggregate_model(self):
        ready_models = [joblib.load(task.get(), mmap_mode='r')
                        for task in self.completed_tasks()]
        if not ready_models:
            return None
        return combine(ready_models)
