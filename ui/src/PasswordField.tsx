import { useId, useState, type InputHTMLAttributes } from 'react'

type PasswordFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'id'> & {
  label: string
}

export function PasswordField({ label, ...inputProps }: PasswordFieldProps) {
  const id = useId()
  const [visible, setVisible] = useState(false)

  return (
    <div className="password-group">
      <label htmlFor={id}>{label}</label>
      <span className="password-field">
        <input {...inputProps} id={id} type={visible ? 'text' : 'password'} />
        <button
          aria-controls={id}
          aria-pressed={visible}
          className="password-toggle"
          onClick={() => setVisible((current) => !current)}
          type="button"
        >
          {visible ? 'Hide' : 'Show'}
        </button>
      </span>
    </div>
  )
}
