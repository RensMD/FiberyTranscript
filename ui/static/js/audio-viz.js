/**
 * Audio level visualization using Canvas.
 * Draws animated level bars with green-yellow-red gradient.
 */
class AudioVisualizer {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
        this.currentLevel = 0;
        this.targetLevel = 0;
        this.smoothing = 0.15; // Smoothing factor for animation
        this.animating = false;
        this._cachedColors = null;
    }

    /** Refresh cached gradient colors from CSS variables. Call on theme change. */
    refreshColors() {
        const style = getComputedStyle(document.body);
        this._cachedColors = {
            green: style.getPropertyValue('--level-green').trim() || '#66bb6a',
            yellow: style.getPropertyValue('--level-yellow').trim() || '#ffa726',
            red: style.getPropertyValue('--level-red').trim() || '#ef5350',
        };
    }

    /**
     * Update the target level (0.0 - 1.0).
     */
    setLevel(level) {
        this.targetLevel = Math.max(0, Math.min(1, level));
        if (!this.animating) {
            this.animating = true;
            this._animate();
        }
    }

    _animate() {
        // Smooth interpolation toward target
        const diff = this.targetLevel - this.currentLevel;
        if (this.targetLevel > this.currentLevel) {
            // Attack: fast rise
            this.currentLevel += diff * 0.4;
        } else {
            // Decay: slower fall
            this.currentLevel += diff * 0.15;
        }

        // Snap to zero if very close
        if (Math.abs(diff) < 0.001) {
            this.currentLevel = this.targetLevel;
        }

        this._draw();

        if (this.currentLevel > 0.001 || this.targetLevel > 0.001) {
            requestAnimationFrame(() => this._animate());
        } else {
            this.animating = false;
            this.currentLevel = 0;
            this._draw(); // Final clear draw
        }
    }

    _draw() {
        const { canvas, ctx, currentLevel } = this;
        const w = canvas.width;
        const h = canvas.height;
        const dpr = window.devicePixelRatio || 1;

        // Handle high-DPI displays
        if (canvas.width !== canvas.clientWidth * dpr) {
            canvas.width = canvas.clientWidth * dpr;
            canvas.height = canvas.clientHeight * dpr;
            ctx.scale(dpr, dpr);
        }

        const drawW = canvas.clientWidth;
        const drawH = canvas.clientHeight;

        ctx.clearRect(0, 0, drawW, drawH);

        if (currentLevel <= 0) return;

        const barWidth = drawW * currentLevel;

        // Create gradient: green → yellow → red (using cached colors)
        if (!this._cachedColors) this.refreshColors();
        const gradient = ctx.createLinearGradient(0, 0, drawW, 0);
        gradient.addColorStop(0, this._cachedColors.green);
        gradient.addColorStop(0.6, this._cachedColors.yellow);
        gradient.addColorStop(1, this._cachedColors.red);

        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.roundRect(0, 0, barWidth, drawH, 3);
        ctx.fill();
    }
}

// Create visualizers for mic and system audio
window.micViz = new AudioVisualizer('micLevelCanvas');
window.sysViz = new AudioVisualizer('sysLevelCanvas');

/**
 * Called from Python backend to update audio levels.
 */
window.updateAudioLevels = function(micLevel, sysLevel) {
    if (micLevel >= 0) window.micViz.setLevel(micLevel);
    if (sysLevel >= 0) window.sysViz.setLevel(sysLevel);
};
