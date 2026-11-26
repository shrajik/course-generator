import { BookOpen, FileText, Image as ImageIcon, Lightbulb, Users } from "lucide-react";

/**
 * The marketing/brand panel shared by every immersive auth screen (login,
 * forgot-password, reset-password) - extracted from AuthCard so the three
 * screens stay visually identical without duplicating this JSX.
 */
export function AuthVisualPanel() {
  return (
    <section className="login-visual" aria-label="AI Course Creator overview">
      <div className="login-visual-content">
        <div className="login-brand"><span className="login-brand-mark"><BookOpen size={19} /></span><span>AI Course Creator</span></div>
        <div className="login-visual-copy">
          <p className="login-kicker">Learn · Create · Grow</p>
          <h1>Turn your ideas<br />into complete<br /><em>courses with AI.</em></h1>
          <p className="login-lede">Research, structure, write and design<br />beautiful courses — in minutes, not months.</p>
          <div className="login-features">
            <div><span><Lightbulb size={17} /></span><p><strong>AI-Powered Content</strong><small>From idea to full course</small></p></div>
            <div><span><FileText size={17} /></span><p><strong>Structured &amp; Comprehensive</strong><small>Well-organized chapters</small></p></div>
            <div><span><ImageIcon size={17} /></span><p><strong>Rich Media Support</strong><small>Images, examples and more</small></p></div>
            <div><span><Users size={17} /></span><p><strong>For Educators, Creators &amp; Teams</strong><small>Share knowledge, make an impact</small></p></div>
          </div>
        </div>
      </div>
      <div className="login-visual-quote">“Knowledge grows<br />when it’s shared.”</div>
    </section>
  );
}
