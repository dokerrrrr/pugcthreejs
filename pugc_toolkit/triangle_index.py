"""CPU bounding-volume hierarchy for ray and nearest-surface queries."""
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count

import numpy as np


@dataclass(slots=True)
class Node:
    low: np.ndarray
    high: np.ndarray
    children: tuple = ()
    indices: np.ndarray | None = None


def intersects(origin, direction, low, high, distance):
    start, end = 0., distance
    for axis in range(3):
        d, o = direction[axis], origin[axis]
        if abs(d) < 1e-15:
            if o < low[axis] or o > high[axis]:
                return False
            continue
        a, b = (low[axis] - o) / d, (high[axis] - o) / d
        start, end = max(start, min(a, b)), min(end, max(a, b))
        if start > end:
            return False
    return True


class TriangleIndex:
    def __init__(self, points, faces):
        self.vertices = np.asarray(points, dtype=float)[faces]
        self.edge_a = self.vertices[:, 1] - self.vertices[:, 0]
        self.edge_b = self.vertices[:, 2] - self.vertices[:, 0]
        self.normals = np.cross(self.edge_a, self.edge_b)
        self.root = None
        if not len(faces):
            return
        low, high = self.vertices.min(axis=1), self.vertices.max(axis=1)
        centers = (low + high) / 2

        def build(indices):
            lower, upper = low[indices].min(0), high[indices].max(0)
            if len(indices) <= 32:
                return Node(lower, upper, indices=indices)
            axis = np.argmax(np.ptp(centers[indices], axis=0))
            half = len(indices) // 2
            split = indices[np.argpartition(centers[indices, axis], half)]
            return Node(lower, upper, (build(split[:half]), build(split[half:])))

        self.root = build(np.arange(len(faces)))

    def result(self, point, index, distance):
        if index is None:
            return None, None, None, None
        normal = self.normals[index]
        norm = np.linalg.norm(normal)
        return point, normal / norm if norm else normal, int(index), float(distance)

    def ray_cast(self, origin, direction, distance=np.inf):
        origin, direction = np.asarray(origin, dtype=float), np.asarray(direction, dtype=float)
        norm = np.linalg.norm(direction)
        maximum, best = distance, None
        if self.root is None or not norm or distance < 0:
            return self.result(None, best, maximum)
        direction = direction / norm
        stack = [self.root]
        while stack:
            node = stack.pop()
            if not intersects(origin, direction, node.low - 1e-9, node.high + 1e-9, maximum):
                continue
            if node.children:
                stack.extend(node.children)
                continue
            idx = node.indices
            a, b = self.edge_a[idx], self.edge_b[idx]
            cross = np.cross(direction, b)
            determinant = (a * cross).sum(1)
            valid = abs(determinant) > 1e-15
            inverse = np.divide(1., determinant, where=valid, out=np.zeros_like(determinant))
            offset = origin - self.vertices[idx, 0]
            u = (offset * cross).sum(1) * inverse
            second = np.cross(offset, a)
            v = (second @ direction) * inverse
            t = (b * second).sum(1) * inverse
            mask = valid & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t >= 0) & (t <= maximum)
            indices = np.flatnonzero(mask)
            if len(indices):
                choice = indices[np.lexsort((idx[indices], t[indices]))[0]]
                if best is None or (t[choice], idx[choice]) < (maximum, best):
                    maximum, best = t[choice], idx[choice]
        return self.result(origin + maximum * direction if best is not None else None, best, maximum)

    def find_nearest(self, origin):
        origin = np.asarray(origin, dtype=float)
        best, point, distance_sq = None, None, np.inf
        serial, queue = count(), []

        def push(node):
            offset = np.maximum(0, np.maximum(node.low - origin, origin - node.high))
            bound = offset @ offset
            if bound <= distance_sq:
                heappush(queue, (bound, next(serial), node))

        if self.root is not None:
            push(self.root)
        while queue:
            lower, _, node = heappop(queue)
            if lower > distance_sq:
                break
            if node.children:
                for child in node.children:
                    push(child)
                continue
            for index in node.indices:
                points = self.vertices[index]
                a, b, normal = self.edge_a[index], self.edge_b[index], self.normals[index]
                norm_sq = normal @ normal
                candidates = []
                if norm_sq > 1e-30:
                    delta = origin - points[0]
                    projection = delta - normal * (delta @ normal) / norm_sq
                    u = np.cross(projection, b) @ normal / norm_sq
                    v = np.cross(a, projection) @ normal / norm_sq
                    if u >= 0 and v >= 0 and u + v <= 1:
                        candidates.append(points[0] + projection)
                for i, j in ((0, 1), (1, 2), (2, 0)):
                    edge = points[j] - points[i]
                    length_sq = edge @ edge
                    fraction = np.clip((origin - points[i]) @ edge / length_sq, 0, 1) if length_sq else 0
                    candidates.append(points[i] + fraction * edge)
                for candidate in candidates:
                    delta = candidate - origin
                    square = delta @ delta
                    if best is None or (square, index) < (distance_sq, best):
                        best, point, distance_sq = index, candidate, square
        return self.result(point, best, np.sqrt(distance_sq))
