import logging
import sys
import time

import matplotlib.pyplot as plt
import numpy as np
import yaml
from utils.utils import try_except

from .result import Result

logging.basicConfig(format='%(message)s', level=logging.INFO)
LOGGER = logging.getLogger(__name__)

# lower lim, upper lim, pace
META = {
    'unique': {'weight_decay': [0, 1, 1e-5],
               'fl_gamma': [0, 3, 1e-3],
               'hsv_h': [0, 0.1, 1e-2],
               'hsv_s': [0, 0.9, 1e-2],
               'hsv_v': [0, 0.9, 1e-2]},
    'general': {'kernel': [1, 99, 2],
                'width': [4, 2048, 4],
                'depth': [1, 10, 1],
                'float': [0, 1e10, 1e-8],
                'uint': [0, 1e10, 1],
                'proba': [0, 1, 1e-2]}
}


class HyperEvolve:

    def set_meta(self):
        if sys.version_info[:2] < (3, 6):
            raise RuntimeError('<dict> in Python versions younger than 3.6.0 are not supported')
        # meta: lower lim, upper lim, pace, patience, greedy momentum
        self.meta = {}
        default_state = [self.patience, 0]
        # 专有超参数
        for key in filter(lambda k: k in self.hyp, META['unique']):
            self.meta[key] = META['unique'][key] + default_state
        # 通用超参数
        for suffix in META['general']:
            for key in filter(lambda k: k.endswith(f'_{suffix}'), self.hyp):
                self.meta[key] = META['general'][suffix] + default_state

    def __init__(self, project, hyp, patience=2):
        self.project = project
        self.project.mkdir(parents=True, exist_ok=True)
        self.title = 'evolve', 'hyp', 'previous', 'current', 'momentum', 'better'
        self.result = Result(self.project, title=self.title[1:-1] + ('fitness',))
        # 读取 yaml 文件并设置为属性
        if not isinstance(hyp, dict):
            hyp = yaml.load(hyp.read_text(), Loader=yaml.Loader)
        self.hyp = hyp
        # 搜索过程变量
        self.patience = patience
        self.set_meta()
        self.seed = round(time.time())

    def save_or_load(self, save=True):
        hyp_f = self.project / 'hyp.yaml'
        state_f = self.project / 'evolve.yaml'
        # 保存状态信息
        if save:
            hyp_f.write_text(yaml.dump(self.hyp))
            meta = self.meta.copy()
            meta.update({'_seed': self.seed})
            state_f.write_text(yaml.dump(meta))
        # 加载状态信息
        else:
            if state_f.is_file():
                self.meta = yaml.load(state_f.read_text(), Loader=yaml.Loader)
                for key in filter(lambda k: k.startswith('_'), tuple(self.meta)):
                    setattr(self, key[1:], self.meta.pop(key))
            if hyp_f.is_file():
                self.hyp = yaml.load(hyp_f.read_text(), Loader=yaml.Loader)

    def __call__(self, fitness, epochs, mutation=.5):
        ''' fitness(hyp, epoch) -> float: 适应度函数
            epochs: 超参数进化总轮次
            mutation: 基因突变时的浮动百分比'''
        epoch, best_fit = len(self.result) - 1, self.result['fitness'].max()
        self.save_or_load(save=False)
        LOGGER.info(f'Evolving hyperparameters: {list(self.meta)}')
        # 获取 baseline 的 fitness
        if epoch == -1:
            epoch += 1
            best_fit = fitness(self.hyp, 0)
            LOGGER.info(f'The fitness of baseline: {best_fit:.4g}\n')
            self.result.record(('baseline', 0, 0, 0, best_fit), epoch)
        # 保存当前状态信息
        self.save_or_load(save=True)
        while epoch < epochs and self.meta:
            # 设置随机数种子, 确保训练中断时可恢复
            self.seed += 1
            np.random.seed(self.seed)
            # 随机选取一个超参数
            p = np.array([self.meta[key][-2] for key in self.meta]) ** 2
            key = np.random.choice(tuple(self.meta), p=p / p.sum())
            # 根据当前值进行搜索
            lower_lim, upper_lim, pace, patience, m_greed = self.meta[key]
            range_ = round((upper_lim - lower_lim) / pace)
            previous = round((self.hyp[key] - lower_lim) / pace)
            # 根据是否到达上 / 下限确定动量
            m_forced = {0: 1, range_: -1}.get(previous, 0)
            momentum = np.random.choice((-1, 1)) if not (m_greed or m_forced) else np.sign(sum((m_greed, m_forced)))
            # m_greed, m_forced 符号相反时不再搜索
            skip = not momentum
            if not skip:
                epoch += 1
                current = np.clip(previous + momentum *
                                  np.random.randint(1, max((1, round(previous * mutation))) + 1),
                                  a_min=0, a_max=range_).item()
                momentum = round(np.clip((current - previous) / (previous + 1e-8), a_min=-1, a_max=1).item(), 3)
                # 修改超参数字典
                hyp = self.hyp.copy()
                hyp[key] = current * pace + lower_lim
                LOGGER.info(f'\n{type(self).__name__} epoch {epoch}: '
                            f'Change <{key}> from {self.hyp[key]:.4g} to {hyp[key]:.4g}\n')
                # 计算适应度
                fit = fitness(hyp, epoch)
                better = fit - best_fit
                LOGGER.info(('\n' + ' %9s' * 6) % self.title)
                LOGGER.info((' %9s' * 2 + ' %9.4g' * 4) % \
                            (f'{epoch}/{epochs}', key[:9], self.hyp[key], hyp[key], momentum, better) + '\n')
                self.result.record((key, self.hyp[key], hyp[key], momentum, fit), epoch)
                # 保存状态信息
                patience += 1 if better > 0 else -0.5
                self.meta[key][-2:] = min((2 * self.patience, patience)), int(np.sign(momentum * better).item())
                skip = patience <= 0
                if better > 0: self.hyp, best_fit = hyp, fit
            # 结束该超参数的搜索
            if skip: self.meta.pop(key)
            self.save_or_load(save=True)
        best = self.result['fitness'].argmax()
        LOGGER.info(f'Hyperparameter evolution is complete, the best epoch is {best}.\n')
        self.plot(show=False)
        return self.hyp

    @try_except
    def plot(self, show=True):
        # 对各个超参数进行分类, 记录最优适应度曲线
        hyp, best = {}, [(0, self.result.loc[0, 'fitness'])]
        for i in range(1, len(self.result)):
            r = self.result.iloc[i]
            p = i, r['fitness']
            hyp.setdefault(r['hyp'], []).append(p + (r['momentum'],))
            if p[1] >= best[-1][1]: best.append(p)
        # 绘制最优适应度曲线
        best = np.array(best).T
        for i in 'xy': getattr(plt, f'{i}ticks')([], [])
        plt.plot(*best, color='gray', alpha=.5, linestyle='--')
        # 绘制各个超参数的散点
        for k in sorted(hyp):
            v = np.array(hyp[k]).T
            alpha = (v[-1] + 1) * .35 + .3
            plt.scatter(*v[:2], alpha=alpha, label=k)
        # 根据最优适应度曲线设置上下限
        # plt.ylim((best[1, 0], best[1, -1]))
        plt.legend(frameon=True)
        plt.savefig(self.project / 'curve.png')
        plt.show() if show else plt.close()


if __name__ == '__main__':
    from pathlib import Path


    def fitness(hyp, epoch=None):
        loss = 0
        for key, (low_lim, up_lim, pace) in META['unique'].items():
            if key in hyp:
                loss += (hyp[key] / up_lim - 0.5) ** 2
        return - loss - (hyp['gb_kernel'] - 4) ** 2


    evolve = HyperEvolve(Path('__pycache__'), Path('../config/hyp.yaml'))
    print(evolve(fitness, 300))
