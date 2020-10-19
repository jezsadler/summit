from summit.strategies.base import Strategy
from summit.strategies.random import LHS
from summit.domain import *
from summit.utils.dataset import DataSet

from entmoot.optimizer.optimizer import Optimizer

import numpy as np
import pandas as pd
from abc import ABC, abstractmethod


class TreeBayes(Strategy):
    """Single-objective Bayesian Optimization (SOBO)

    This is a general BO method since it is a wrapper around GPyOpt.

    Parameters
    ----------
    domain: :class:`~summit.domain.Domain`
        The Summit domain describing the optimization problem.
    transform : :class:`~summit.strategies.base.Transform`, optional
        A transform object. By default no transformation will be done
        on the input variables or objectives.
    estimator_type: string, optional
        The ENTMOOT base_estimator type.
        By default, Gradient-Boosted Regression
    std_estimator_type: string, optional
        The ENTMOOT std_estimator
        By default, bounded data distance
    acquisition_type: string, optional
        The acquisition function type from ENTMOOT. See notes for options.
        By default, Lower Confidence Bound.
    optimizer_type: string, optional
        The optimizer used in ENTMOOT for maximization of the acquisition function.
        By default, sampling will be used.
    generator_type: string, optional
        The method for generating initial points before a model can be trained.
        By default, uniform random points will be used.
    initial_points: int, optional
        How many points to require before training models
    min_child_samples: int, optional
        Minimum size of a leaf in tree models

[note: do we need these ones?]
    use_descriptors : bool, optional
        Whether to use descriptors of categorical variables. Defaults to False.
    exact_feval: boolean, optional
        Whether the function evaluations are exact (True) or noisy (False).
        By default: False.
    ARD: boolean, optional
        Whether automatic relevance determination should be applied (True).
        By default: True.
    standardize_outputs: boolean, optional
        Whether the outputs should be standardized (True).
        By default: True.

    Examples
    --------
    >>> from summit.domain import *
    >>> from summit.strategies import SOBO
    >>> import numpy as np
    >>> domain = Domain()
    >>> domain += ContinuousVariable(name='temperature', description='reaction temperature in celsius', bounds=[50, 100])
    >>> domain += CategoricalVariable(name='flowrate_a', description='flow of reactant a in mL/min', levels=[1,2,3,4,5])
    >>> domain += ContinuousVariable(name='flowrate_b', description='flow of reactant b in mL/min', bounds=[0.1, 0.5])
    >>> domain += ContinuousVariable(name='yield', description='yield of reaction', bounds=[0,100], is_objective=True)
    >>> strategy = TreeBayes(domain)
    >>> next_experiments = strategy.suggest_experiments(5)

    Notes
    ----------

    Estimator type
        GBRT: Gradient-boosted regression trees

        RF: random forest (scikit-learn)

    Acquisition function type
        LCB: lower confidence bound


    """

    def __init__(
        self,
        domain,
        transform=None,
        estimator_type=None,
        std_estimator_type=None,
        acquisition_type=None,
        optimizer_type=None,
        generator_type=None,
        initial_points=50,
        min_child_samples=5,
        **kwargs
    ):
        Strategy.__init__(self, domain, transform=transform, **kwargs)

        self.use_descriptors = kwargs.get("use_descriptors", False)
        # TODO: notation - discrete in our model (e.g., catalyst type) = categorical?
        self.input_domain = []
        for v in self.domain.variables:
            if not v.is_objective:
                if isinstance(v, ContinuousVariable):
                    self.input_domain.append(
                        {
                            "name": v.name,
                            "type": v.variable_type,
                            "domain": (v.bounds[0], v.bounds[1]),
                        }
                    )
                elif isinstance(v, CategoricalVariable):
                    if not self.use_descriptors:
                        self.input_domain.append(
                            {
                                "name": v.name,
                                "type": "categorical",
                                "domain": tuple(self.categorical_wrapper(v.levels)),
                            }
                        )
                    elif v.ds is not None and self.use_descriptors:
                        if v.ds is None:
                            raise ValueError(
                                "No descriptors provided for variable: {}".format(
                                    v.name
                                )
                            )
                        descriptor_names = v.ds.data_columns
                        descriptors = np.asarray(
                            [
                                v.ds.loc[:, [l]].values.tolist()
                                for l in v.ds.data_columns
                            ]
                        )
                        for j, d in enumerate(descriptors):
                            self.input_domain.append(
                                {
                                    "name": descriptor_names[j],
                                    "type": "continuous",
                                    "domain": (
                                        np.min(np.asarray(d)),
                                        np.max(np.asarray(d)),
                                    ),
                                }
                            )
                    elif v.ds is None and self.use_descriptors:
                        raise ValueError(
                            "Cannot use descriptors because none are provided."
                        )
                    # TODO: GPyOpt currently does not support mixed-domains w/ bandit inputs, there is a PR for this though
                else:
                    raise TypeError("Unknown variable type.")

        # TODO: how to handle equality constraints? Could we remove '==' from constraint types as each equality
        #  constraint reduces the degrees of freedom?
        if self.domain.constraints is not None:
            constraints = self.constr_wrapper(self.domain)
            self.constraints = [
                {
                    "name": "constr_" + str(i),
                    "constraint": c[0]
                    if c[1] in ["<=", "<"]
                    else "(" + c[0] + ")*(-1)",
                }
                for i, c in enumerate(constraints)
                if not (c[1] == "==")
            ]
        else:
            self.constraints = None

        self.input_dim = len(self.domain.input_variables)

        if estimator_type in [
            "GBRT",
            "RF",
        ]:
            self.estimator_type = estimator_type
        else:
            self.estimator_type = "GBRT"  # default model type is GB trees


        if std_estimator_type in [
            "BDD",
            "L1BDD",
            "DDP",
            "L1DDP",
        ]:
            self.std_estimator_type = std_estimator_type
        else:
            self.std_estimator_type = "BDD"  # default model type is bounded data distance


        if acquisition_type in [
            "LCB",
        ]:
            self.acquisition_type = acquisition_type
        else:
            self.acquisition_type = (
                "LCB"  # default acquisition function is lower confidence bound
            )

        """ 
        Method for optimization of acquisition function
           sampling: optimized by computing `acquisition_type` at `n_points` 
               randomly sampled points
           global: optimized by using global solver to find minimum of 
               `acquisition_type`. Requires gurobipy
        """
        if optimizer_type in ["sampling","global"]:
            self.optimizer_type = optimizer_type
        else:
            self.optimizer_type = "sampling"  # default optimizer: sampling
        """
        Sets an initial points generator. Can be either
        - "random" for uniform random numbers,
        - "sobol" for a Sobol sequence,
        - "halton" for a Halton sequence,
        - "hammersly" for a Hammersly sequence,
        - "lhs" for a latin hypercube sequence,
        - "grid" for a uniform grid sequence
        """
        if generator_type in [
                "random",
                "sobol",
                "halton",
                "hammersly",
                "lhs",
                "grid",
                ]:
            self.generator_type = generator_type
        else:
            self.generator_type = "random"

        self.initial_points = initial_points
        self.min_child_samples = min_child_samples
        self.prev_param = None


    def suggest_experiments(
        self, num_experiments=1, prev_res: DataSet = None, **kwargs
    ):
        """Suggest experiments using ENTMOOT tree-based Bayesian Optimization

        Parameters
        ----------
        num_experiments: int, optional
            The number of experiments (i.e., samples) to generate. Default is 1.
        prev_res: :class:`~summit.utils.data.DataSet`, optional
            Dataset with data from previous experiments of previous iteration.
            If no data is passed, then random sampling will
            be used to suggest an initial design.

        Returns
        -------
        next_experiments : :class:`~summit.utils.data.DataSet`
            A Dataset object with the suggested experiments

        """

        param = None
        xbest = np.zeros(self.domain.num_continuous_dimensions())
        obj = self.domain.output_variables[0]
        objective_dir = -1.0 if obj.maximize else 1.0
        fbest = float("inf")


        bounds = [k["domain"] for k in self.input_domain]
        entmoot_model = Optimizer(
            dimensions=bounds,
            base_estimator=self.estimator_type,
            std_estimator=self.std_estimator_type, 
            n_initial_points=self.initial_points, 
            initial_point_generator=self.generator_type,
            acq_func=self.acquisition_type, 
            acq_optimizer=self.optimizer_type, 
            random_state=None, 
            acq_func_kwargs=None, 
            acq_optimizer_kwargs=None,
            base_estimator_kwargs={"min_child_samples":self.min_child_samples},
            std_estimator_kwargs=None,
            model_queue_size=None,
            verbose=True
            )

        # If we have previous results:
        if prev_res is not None:
            # Get inputs and outputs
            inputs, outputs = self.transform.transform_inputs_outputs(
                prev_res, transform_descriptors=self.use_descriptors
            )

            # Set up maximization and minimization by converting maximization to minimization problem
            for v in self.domain.variables:
                if v.is_objective and v.maximize:
                    outputs[v.name] = -1 * outputs[v.name]
                if isinstance(v, CategoricalVariable):
                    if not self.use_descriptors:
                        inputs[v.name] = self.categorical_wrapper(
                            inputs[v.name], v.levels
                        )

            inputs = inputs.to_numpy()
            outputs = outputs.to_numpy()
            
            if self.prev_param is not None:
                X_step = self.prev_param[0]
                Y_step = self.prev_param[1]

                X_step = np.vstack((X_step, inputs))
                Y_step = np.vstack((Y_step, outputs))

            else:
                X_step = inputs
                Y_step = outputs
            # Convert to list form to give to optimizer
            prev_X = [list(x) for x in X_step]
            prev_y = [y for x in Y_step for y in x]
             
            # Train entmoot model
            entmoot_model.tell(prev_X,prev_y,fit=True)
            
            # Store parameters (history of suggested points and function evaluations)
            param = [X_step, Y_step]
            fbest = np.min(Y_step)
            xbest = X_step[np.argmin(Y_step)]
        
        request=np.array(entmoot_model.ask(n_points=num_experiments,strategy="cl_mean"))
        # Generate DataSet object with variable values of next
        next_experiments = None
        transform_descriptors = False
        if request is not None and len(request) != 0:
            next_experiments = {}
            i_inp = 0
            for v in self.domain.variables:
                if not v.is_objective:
                    if isinstance(v, CategoricalVariable):
                        if v.ds is None or not self.use_descriptors:
                            cat_list = []
                            for j, entry in enumerate(request[:, i_inp]):
                                cat_list.append(
                                    self.categorical_unwrap(entry, v.levels)
                                )
                            next_experiments[v.name] = np.asarray(cat_list)
                            i_inp += 1
                        else:
                            descriptor_names = v.ds.data_columns
                            for d in descriptor_names:
                                next_experiments[d] = request[:, i_inp]
                                i_inp += 1
                            transform_descriptors = True
                    else:
                        next_experiments[v.name] = request[:, i_inp]
                        i_inp += 1
            next_experiments = DataSet.from_df(pd.DataFrame(data=next_experiments))
            next_experiments[("strategy", "METADATA")] = "ENTMOOT"

        self.fbest = objective_dir * fbest
        self.xbest = xbest
        self.prev_param = param

        # Do any necessary transformation back
        next_experiments = self.transform.un_transform(
            next_experiments, transform_descriptors=self.use_descriptors
        )

        return next_experiments

    def reset(self):
        """Reset the internal parameters"""
        self.prev_param = None

"""    def to_dict(self):
        if self.prev_param is not None:
            param = [self.prev_param[0].tolist(), self.prev_param[1].tolist()]
        else:
            param = None

        strategy_params = dict(
            prev_param=param,
            use_descriptors=self.use_descriptors,
            estimator_type=self.estimator_type,
            acquisition_type=self.acquisition_type,
            optimizer_type=self.optimizer_type,
            evaluator_type=self.evaluator_type,
            kernel=self.kernel.to_dict(),
            exact_feval=self.exact_feval,
            ARD=self.ARD,
            standardize_outputs=self.standardize_outputs,
        )

        return super().to_dict(**strategy_params)
"""
"""
    @classmethod
    def from_dict(cls, d):
        # Get kernel
        kernel = d["strategy_params"].get("kernel")
        if kernel is not None:
            kernel = GPy.kern.Kern.from_dict(kernel)
            d["strategy_params"]["kernel"] = kernel

        # Setup SOBO
        sobo = super().from_dict(d)
        param = d["strategy_params"]["prev_param"]
        if param is not None:
            param = [np.array(param[0]), np.array(param[1])]
            sobo.prev_param = param
        return sobo
"""
"""
    def constr_wrapper(self, summit_domain):
        v_input_names = [v.name for v in summit_domain.variables if not v.is_objective]
        gpyopt_constraints = []
        for c in summit_domain.constraints:
            tmp_c = c.lhs
            for v_input_index, v_input_name in enumerate(v_input_names):
                v_gpyopt_name = "x[:," + str(v_input_index) + "]"
                tmp_c = tmp_c.replace(v_input_name, v_gpyopt_name)
            gpyopt_constraints.append([tmp_c, c.constraint_type])
        return gpyopt_constraints
"""
"""
    def categorical_wrapper(self, categories, reference_categories=None):
        if not reference_categories:
            return [i for i, _ in enumerate(categories)]
        else:
            return [reference_categories.index(c) for c in categories]

    def categorical_unwrap(self, gpyopt_level, categories):
        return categories[int(gpyopt_level)]
"""