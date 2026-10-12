# SPDX-License-Identifier: MPL-2.0
"""Bounded, data-only prototype expressions. Never execute imported source code."""
import math


def evaluate(expression,variables,depth=0):
    if depth>48:raise ValueError('Prototype expression is nested too deeply')
    if not isinstance(expression,dict):raise ValueError('Invalid prototype expression')
    if 'literal' in expression:return expression['literal']
    if 'variable' in expression:
        name=expression['variable']
        if name not in variables:raise ValueError('Prototype variable is missing: '+name)
        return variables[name]['value']
    operation=expression.get('operation');arguments=expression.get('arguments',[])
    def arg(index):
        if index>=len(arguments):raise ValueError('Prototype expression is missing an argument')
        return evaluate(arguments[index],variables,depth+1)
    if operation=='TERNARY':return arg(1) if bool(arg(0)) else arg(2)
    if operation=='AND':return bool(arg(0)) and bool(arg(1))
    if operation=='OR':return bool(arg(0)) or bool(arg(1))
    if operation=='NOT':return not bool(arg(0))
    if operation=='IS_TRUTHY':return bool(arg(0))
    if operation=='STRINGIFY':return str(arg(0))
    if operation=='NEGATE':return -arg(0)
    left,right=arg(0),arg(1)
    operations={'ADDITION':lambda:left+right,'SUBTRACTION':lambda:left-right,'MULTIPLY':lambda:left*right,
        'DIVIDE':lambda:left/right,'EQUALS':lambda:left==right,'NOT_EQUAL':lambda:left!=right,
        'LESS_THAN':lambda:left<right,'LESS_THAN_OR_EQUAL':lambda:left<=right,
        'GREATER_THAN':lambda:left>right,'GREATER_THAN_OR_EQUAL':lambda:left>=right}
    if operation not in operations:raise ValueError('Unsupported prototype expression: '+str(operation))
    try:value=operations[operation]()
    except (TypeError,ZeroDivisionError,OverflowError) as error:raise ValueError('Invalid prototype expression operands') from error
    if isinstance(value,(int,float)) and not math.isfinite(value):raise ValueError('Prototype expression is not finite')
    return value
