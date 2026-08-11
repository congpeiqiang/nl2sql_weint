import sys
sys.path.insert(0, '.')

def pack(kwargs, engine):
    kwargs = dict(kwargs)
    if engine == 'echarts':
        if 'echarts' in kwargs and 'echartsOption' not in kwargs:
            kwargs['echartsOption'] = kwargs.pop('echarts')
        if 'outputType' not in kwargs or kwargs.get('outputType') in (None, '', 'png'):
            kwargs['outputType'] = 'svg'
        return kwargs
    return kwargs

print('TEST1:', pack({'width':1000,'height':500,'echarts':'{x}'}, 'echarts'))
print('TEST2:', pack({'width':1000,'height':500,'echartsOption':'{x}','outputType':'png'}, 'echarts'))
print('TEST3:', pack({'width':1000,'height':500,'echartsOption':'{x}','outputType':'svg'}, 'echarts'))
