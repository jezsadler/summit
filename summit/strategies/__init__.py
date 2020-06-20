from .base import Strategy, Transform, MultitoSingleObjective, LogSpaceObjectives
from .random import Random, LHS
from .tsemo import TSEMO2
from .neldermead import NelderMead
from .snobfit import SNOBFIT
from .sobo import SOBO

def strategy_from_dict(d):
    if d["name"] == TSEMO2:
        return TSEMO2.from_dict(d)
    elif d["name"] == SNOBFIT:
        return SNOBFIT.from_dict(d)
    elif d["name"] == NelderMead:
        return NelderMead.from_dict(d)