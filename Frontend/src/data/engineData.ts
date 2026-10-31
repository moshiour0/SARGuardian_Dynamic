export type Engine = {
  id: 'landslide' | 'glacier' | 'known-place' | 'river-shift';
  name: string;
  status: 'AVAILABLE' | 'RESEARCH PREVIEW';
  description: string;
  actionLabel: string;
  image: string;
  icon: 'landslide' | 'glacier' | 'known-place' | 'river';
};

export const engines: Engine[] = [
  {
    id: 'landslide',
    name: 'Landslide',
    status: 'AVAILABLE',
    description: 'Screen the full area for spatial change candidates, then inspect only measurements supported by the observations.',
    actionLabel: 'Open area analysis',
    image: 'https://images.unsplash.com/photo-1464822759023-fed622ff2c3b?auto=format&fit=crop&w=900&q=88',
    icon: 'landslide',
  },
  {
    id: 'glacier',
    name: 'Glacier Movement',
    status: 'AVAILABLE',
    description: 'Use the same AOI-wide displacement workflow to examine surface-change evidence across glacier terrain.',
    actionLabel: 'Explore glacier terrain',
    image: 'https://images.unsplash.com/photo-1454496522488-7a8e488e8606?auto=format&fit=crop&w=900&q=88',
    icon: 'glacier',
  },
  {
    id: 'known-place',
    name: 'Known Place Analysis',
    status: 'AVAILABLE',
    description: 'Start from a known location or measurement target for a focused, separate research workflow.',
    actionLabel: 'Select an area and target',
    image: 'https://images.unsplash.com/photo-1519681393784-d120267933ba?auto=format&fit=crop&w=900&q=88',
    icon: 'known-place',
  },
  {
    id: 'river-shift',
    name: 'River Shift',
    status: 'RESEARCH PREVIEW',
    description: 'A dedicated channel-position and river-migration workflow is being developed and validated.',
    actionLabel: 'View research preview',
    image: 'https://images.unsplash.com/photo-1437482078695-73f5ca6c96e2?auto=format&fit=crop&w=900&q=88',
    icon: 'river',
  },
];
