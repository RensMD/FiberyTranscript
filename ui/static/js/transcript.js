/**
 * Transcript data manager.
 * Stores diarized transcript in a hidden DOM container for copy/send operations.
 */
class TranscriptManager {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this._cleanedText = null;
    }

    /**
     * Store a diarized transcript with speaker labels.
     */
    setDiarizedTranscript(utterances) {
        this.container.innerHTML = '';
        this._cleanedText = null;

        for (const u of utterances) {
            const label = document.createElement('span');
            label.className = 'speaker-label';
            label.textContent = `Speaker ${u.speaker}`;
            this.container.appendChild(label);

            const text = document.createElement('p');
            text.className = 'final';
            text.textContent = u.text;
            this.container.appendChild(text);
        }
    }

    /**
     * Get all transcript text as plain string.
     */
    getFullText() {
        const elements = this.container.querySelectorAll('.final');
        return Array.from(elements).map(el => el.textContent).join('\n');
    }

    /**
     * Store a cleaned transcript (markdown string from Gemini cleanup).
     * Replaces the raw diarized content.
     */
    setCleanedTranscript(markdownText) {
        this._cleanedText = markdownText;
        // Replace container with cleaned version
        this.container.innerHTML = '';
        const div = document.createElement('div');
        div.className = 'cleaned-transcript';
        div.textContent = markdownText;
        this.container.appendChild(div);
    }

    /**
     * Get diarized text with speaker labels.
     * Returns cleaned transcript if available, otherwise raw diarized format.
     */
    getFormattedText() {
        if (this._cleanedText) {
            return this._cleanedText;
        }
        let text = '';
        const children = this.container.children;
        for (const child of children) {
            if (child.classList.contains('speaker-label')) {
                text += '\n**' + child.textContent + '**\n';
            } else if (child.classList.contains('final')) {
                text += child.textContent + '\n';
            }
        }
        return text.trim();
    }

    /**
     * Clear all transcript content.
     */
    clear() {
        this.container.innerHTML = '';
        this._cleanedText = null;
    }
}

window.transcriptManager = new TranscriptManager('transcriptContent');

// Called from Python backend
window.setDiarizedTranscript = function(utterances) {
    window.transcriptManager.setDiarizedTranscript(utterances);
};

window.setCleanedTranscript = function(cleanedMarkdown) {
    window.transcriptManager.setCleanedTranscript(cleanedMarkdown);
};
